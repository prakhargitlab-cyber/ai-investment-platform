# Test-only, deny-by-default kubectl adapter. Never invokes kubectl or a network API.
function Initialize-LifecycleTransport {
    param($State)
    $script:State = $State
}

function Invoke-LifecycleDocker {
    param([string[]]$Arguments)
    # No current Docker context, remote DOCKER_HOST, shell, or PATH lookup.
    # Correct Windows native quoting also preserves SQL double quotes in PS 5.1.
    $quoted = foreach ($value in (@('--host','npipe:////./pipe/docker_engine') + $Arguments)) {
        '"' + (($value -replace '(\\*)"', '$1$1\"') -replace '(\\+)$', '$1$1') + '"'
    }
    $info = New-Object Diagnostics.ProcessStartInfo
    $info.FileName = $script:State.Docker
    $info.Arguments = $quoted -join ' '
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $process = New-Object Diagnostics.Process
    $process.StartInfo = $info
    [void]$process.Start()
    $stdout = $process.StandardOutput.ReadToEndAsync()
    $stderr = $process.StandardError.ReadToEndAsync()
    if (-not $process.WaitForExit(120000)) { $process.Kill(); throw 'Isolated Docker command timed out' }
    $result = [pscustomobject]@{ExitCode=$process.ExitCode; Output=$stdout.Result; Error=$stderr.Result}
    $process.Dispose()
    return $result
}

function Invoke-LifecycleExec {
    param([string[]]$Arguments)
    $result = Invoke-LifecycleDocker (@('exec', $script:State.Container) + $Arguments)
    return $result
}

function Invoke-LifecycleSql {
    param([string]$Sql, [string]$Database='fixture')
    $r = Invoke-LifecycleExec @('psql','-XqAt','-v','ON_ERROR_STOP=1','-U','tester','-d',$Database,'-c',$Sql)
    if ($r.ExitCode -ne 0) { throw "Fixture SQL failed: $($r.Error)" }
    return $r.Output.Trim()
}

function Set-LifecycleWriterSessions {
    param([string]$Service, [int]$Replicas)
    if ($Service -notin @('writer_one','writer_two') -or $Replicas -notin @(0,1,2)) { throw 'ISOLATION: invalid writer fixture' }
    $application = "stage2d_$Service"
    Invoke-LifecycleSql "select pg_terminate_backend(pid) from pg_stat_activity where application_name='$application';" postgres | Out-Null
    for ($i=0; $i -lt $Replicas; $i++) {
        # Actual writer transactions hold INSERT locks until scale-down terminates
        # them. The uncommitted rows roll back; fixture row counts remain stable.
        $r = Invoke-LifecycleDocker @('exec','-d',$script:State.Container,'env',"PGAPPNAME=$application",'psql','-XqAt','-U','tester','-d','fixture','-c',
            'BEGIN; INSERT INTO public.writer_probe DEFAULT VALUES; SELECT pg_sleep(3600);')
        if ($r.ExitCode -ne 0) { throw "Unable to start isolated writer: $($r.Error)" }
    }
    for ($i=0; $i -lt 20; $i++) {
        $count=Invoke-LifecycleSql "select count(*) from pg_stat_activity where application_name='$application' and state='active';" postgres
        if ([int]$count -eq $Replicas) { return }
        Start-Sleep -Milliseconds 100
    }
    throw 'Isolated writer sessions did not reach the expected count'
}

function Assert-LifecycleLocalPath {
    param([string]$Path)
    $full = [IO.Path]::GetFullPath($Path)
    $prefix = [IO.Path]::GetFullPath($script:State.Root).TrimEnd('\') + '\'
    if (-not $full.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) { throw 'ISOLATION: local path escaped scratch root' }
    return $full
}

function Invoke-IsolatedKubectl {
    param([string[]]$Arguments)
    $s = $script:State
    $s.Events.Add(($Arguments -join ' '))
    $global:LASTEXITCODE = 0
    $a = $Arguments
    if (($a -join ' ') -eq 'config current-context') { return 'stage2d-isolated' }
    if ($a[0] -eq 'cp') {
        if ($a.Count -ne 3) { throw 'ISOLATION: unexpected cp arguments' }
        $pull = $a[1].StartsWith("$($s.Namespace)/$($s.Container):")
        $remote = if ($pull) { $a[1] } else { $a[2] }
        $prefix = "$($s.Namespace)/$($s.Container):"
        if (-not $remote.StartsWith($prefix)) { throw 'ISOLATION: foreign copy target' }
        $path = $remote.Substring($prefix.Length)
        if ($path -notmatch '^/tmp/platform-[A-Za-z0-9_.-]+\.dump$') { throw 'ISOLATION: unexpected remote copy path' }
        $local = Assert-LifecycleLocalPath $(if ($pull) { $a[2] } else { $a[1] })
        $pair = if ($pull) { @("$($s.Container):$path",$local) } else { @($local,"$($s.Container):$path") }
        $r = Invoke-LifecycleDocker (@('cp') + $pair)
        if ($r.ExitCode -ne 0) { throw "Isolated binary copy failed: $($r.Error)" }
        $hash = Invoke-LifecycleExec @('sha256sum',$path)
        if ($hash.ExitCode -ne 0 -or ($hash.Output -split ' ')[0] -ne (Get-FileHash $local -Algorithm SHA256).Hash.ToLowerInvariant()) { throw 'Binary transfer hash mismatch' }
        $s.Transfers++
        if (-not $pull -and $path -like '/tmp/platform-restore-*' -and $s.Fault -eq 'copy-in') { $global:LASTEXITCODE=1 }
        return
    }
    $nsIndex = [Array]::IndexOf($a, '-n')
    if ($nsIndex -lt 0 -or $a[$nsIndex+1] -ne $s.Namespace) { throw 'ISOLATION: foreign or absent namespace' }
    if ($a[0] -eq 'exec') {
        if ($a[1] -ne $s.Container) { throw 'ISOLATION: foreign exec target' }
        $offset = if ($a[4] -eq '--') { 5 } else { 4 } # Functions consume PowerShell's -- token.
        $command = $a[$offset..($a.Length-1)]
        if ($command[0] -notin @('psql','pg_restore','sh','rm')) { throw 'ISOLATION: command not allowed' }
        if ($command[0] -eq 'sh' -and ($command.Count -ne 3 -or $command[2] -notmatch '^printf %s [A-Za-z0-9+/=]+ \| base64 -d \| sh$')) { throw 'ISOLATION: unexpected shell payload' }
        if ($command[0] -eq 'rm' -and ($command.Count -ne 3 -or $command[1] -ne '-f' -or $command[2] -notmatch '^/tmp/platform-[A-Za-z0-9_.-]+\.dump$')) { throw 'ISOLATION: unsafe remote removal' }
        $isRestore = $command[0] -eq 'pg_restore' -and $command -contains '--clean'
        if ($isRestore) {
            $s.Restores++
            $target = $command[[Array]::IndexOf($command,'-d')+1]
            if ($target -eq 'fixture') {
                if (@($s.Replicas.Values | Where-Object { $_ -gt 0 }).Count -or -not (Test-Path $s.Marker)) { throw 'Writer safety invariant failed' }
                if ((Invoke-LifecycleSql "select count(*) from pg_stat_activity where application_name like 'stage2d_writer_%';" postgres) -ne '0') { throw 'Actual writer transactions remain before restore' }
                $s.MarkerPhases.Add((Get-Content $s.Marker -Raw | ConvertFrom-Json).phase)
            }
            if ($s.Fault -eq 'restore') {
                # A real pg_restore error after a real partial database mutation.
                Invoke-LifecycleSql 'TRUNCATE public.payload;' $target | Out-Null
                $command[$command.Length-1] = '/tmp/does-not-exist.dump'
            }
        }
        if ($command[0] -eq 'sh' -and $s.Fault -eq 'backup') {
            # Real snapshot export succeeds, but pg_dump cannot write its output.
            $text = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String(($command[2] -split ' ')[2]))
            $text = $text -replace '-f /tmp/[^ ]+[.]dump', '-f /does-not-exist/failure.dump'
            $command[2] = 'printf %s ' + [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($text)) + ' | base64 -d | sh'
        }
        $r = Invoke-LifecycleExec $command
        if ($isRestore -and $r.ExitCode -eq 0 -and $s.Fault -eq 'missing-table') { Invoke-LifecycleSql 'DROP TABLE public.payload;' $target | Out-Null }
        if ($isRestore -and $r.ExitCode -eq 0 -and $s.Fault -eq 'flyway') { Invoke-LifecycleSql "UPDATE auth.flyway_schema_history SET version='999';" $target | Out-Null }
        $global:LASTEXITCODE = $r.ExitCode
        # Match text-returning kubectl without contaminating valid stdout with NOTICEs.
        if ($r.ExitCode -ne 0) { return ($r.Output + $r.Error) }
        return $r.Output.TrimEnd("`r","`n")
    }
    if ($a[0] -eq 'get' -and $a[1] -eq 'pods' -and $a -contains 'app.kubernetes.io/component=postgres') { return $s.Container }
    if ($a[0] -eq 'get' -and $a[1] -eq 'deployment') {
        if (-not $s.Replicas.ContainsKey($a[2])) { throw 'ISOLATION: unexpected deployment' }
        if ($s.Fault -eq 'replica-query') { $global:LASTEXITCODE=1; return }
        return [string]$s.Replicas[$a[2]]
    }
    if ($a[0] -eq 'scale' -and $a[1] -eq 'deployment') {
        if (-not $s.Replicas.ContainsKey($a[2]) -or $a[-1] -notmatch '^--replicas=([0-9]+)$') { throw 'ISOLATION: unexpected scale request' }
        $replicas = [int]$matches[1]
        if (($s.Fault -eq 'scale-down' -and $replicas -eq 0) -or ($s.Fault -eq 'resume' -and $replicas -gt 0)) { $global:LASTEXITCODE=1; return }
        if (-not (Test-Path $s.Marker)) { throw 'Scale attempted without durable maintenance marker' }
        Set-LifecycleWriterSessions -Service $a[2] -Replicas $replicas
        $s.Replicas[$a[2]] = $replicas
        return
    }
    if ($a[0] -eq 'wait' -or ($a[0] -eq 'get' -and $a[1] -eq 'pod')) {
        $selector = $a[[Array]::IndexOf($a,'-l')+1]
        if ($selector -notmatch '^app.kubernetes.io/component=(writer_one|writer_two)$') { throw 'ISOLATION: wrong writer selector' }
        if ($a[0] -eq 'get' -and $a -notcontains 'status.phase!=Succeeded,status.phase!=Failed') { throw 'ISOLATION: unexpected writer phase filter' }
        if ($a[0] -eq 'wait' -and $s.Fault -eq 'wait') { $global:LASTEXITCODE=1; return }
        if ($a[0] -eq 'get' -and $s.Fault -eq 'remaining') { return 'writer-still-running' }
        $service=$selector.Substring($selector.IndexOf('=')+1)
        $count=Invoke-LifecycleSql "select count(*) from pg_stat_activity where application_name='stage2d_$service';" postgres
        if ($count -ne '0') { return 'actual-writer-session-still-running' }
        return
    }
    throw "ISOLATION: kubectl operation denied: $($a -join ' ')"
}

Export-ModuleMember -Function Initialize-LifecycleTransport, Invoke-LifecycleDocker, Invoke-LifecycleExec, Invoke-LifecycleSql, Invoke-IsolatedKubectl, Set-LifecycleWriterSessions
