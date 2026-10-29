# Real compatibility loader and counter/probe functions; all native I/O is mocked.
$ErrorActionPreference='Stop'
$root=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../../..'))
. (Join-Path $root 'scripts/database/DeploymentSafety.ps1')
$script:Passed=0
function Assert($Value,$Message) { if (-not $Value) { throw $Message } }
function Test($Name,[scriptblock]$Body) { & $Body; $script:Passed++; Write-Output "PASS $Name" }
function Throws([scriptblock]$Body,$Pattern) { try { & $Body } catch { Assert ($_.Exception.Message -like $Pattern) "Unexpected error: $($_.Exception.Message)"; return }; throw 'Expected rejection' }
Test 'compatibility loader includes the REJECT counter dependency' { Assert ([bool](Get-Command Get-VerifiedResearchRejectCounter -CommandType Function)) 'Counter helper missing' }
$script:Gate=@{ResearchPod='research-engine-reviewed';ResearchUID='reviewed-uid';PodIP='10.42.2.51';ServiceIP='10.43.139.131';Callers=@('reviewed-caller')}
$script:ActiveIsolationGate=$script:Gate
function Reset-Proof {
    $script:Pod=@{metadata=@{uid='reviewed-uid'};spec=@{nodeName='k3d-ai-investment-dev-server-0'};status=@{podIP='10.42.2.51';phase='Running'}}
    $script:Firewall=@(
        '-A KUBE-ROUTER-FORWARD -d 10.42.2.51/32 -m comment --comment "reviewed pod" -j KUBE-POD-FW-ABC'
        '-A KUBE-POD-FW-ABC -d 10.42.2.51/32 -m comment --comment "research-bootstrap-isolation" -j KUBE-NWPLCY-DEF'
        '-A KUBE-POD-FW-ABC -m comment --comment "deny ingress" -j REJECT --reject-with icmp-port-unreachable'
    ) -join [Environment]::NewLine
    $script:Counters=[Collections.Generic.Queue[long]]::new(); $script:Counters.Enqueue(7); $script:Counters.Enqueue(9)
    $script:NativeCalls=@(); $script:CounterError=$false; $script:DuplicateCounter=$false
    $script:Probe=@{ExitCode=1;StdOut='';StdErr='bash: /dev/tcp/10.42.2.51/8000: Connection refused; command terminated with exit code 1'}
}
function Invoke-DeploymentKubectl { param($Arguments); $script:Pod | ConvertTo-Json -Depth 8 -Compress }
function Invoke-DeploymentNativeCommand {
    param($Program,$Arguments)
    $script:NativeCalls+=,@{program=$Program;arguments=$Arguments}
    if ($Program -eq 'kubectl') { return $script:Probe }
    Assert ($Program -ceq 'docker' -and $Arguments[1] -ceq 'k3d-ai-investment-dev-server-0') 'Wrong runtime/node'
    if ($Arguments[2] -ceq 'iptables-save') { return @{ExitCode=0;StdOut=$script:Firewall;StdErr=''} }
    if ($Arguments[2] -ceq 'iptables') {
        if ($script:CounterError) { return @{ExitCode=1;StdOut='';StdErr='unavailable'} }
        $text='  '+$script:Counters.Dequeue()+' 420 REJECT all -- * * 0.0.0.0/0 0.0.0.0/0 reject-with icmp-port-unreachable'
        if ($script:DuplicateCounter) { $text+=[Environment]::NewLine+$text }
        return @{ExitCode=0;StdOut=$text;StdErr=''}
    }
    throw 'Unmocked native boundary'
}
Test 'counter resolves reviewed pod/node, firewall chain and exact REJECT count' { Reset-Proof; Assert ((Get-VerifiedResearchRejectCounter $script:Gate) -eq 7) 'Counter mismatch' }
Test 'pod recreation rejected before firewall I/O' { Reset-Proof; $script:Pod.metadata.uid='other'; Throws { Get-VerifiedResearchRejectCounter $script:Gate } '*identity drift*'; Assert ($script:NativeCalls.Count -eq 0) 'Unexpected runtime read' }
Test 'changed pod IP rejected' { Reset-Proof; $script:Pod.status.podIP='10.42.2.99'; Throws { Get-VerifiedResearchRejectCounter $script:Gate } '*identity drift*' }
Test 'unknown node rejected' { Reset-Proof; $script:Pod.spec.nodeName='unknown'; Throws { Get-VerifiedResearchRejectCounter $script:Gate } '*Unexpected research node*' }
Test 'missing pod routing chain rejected' { Reset-Proof; $script:Firewall=''; Throws { Get-VerifiedResearchRejectCounter $script:Gate } '*routing chain absent*' }
Test 'missing policy chain rejected' { Reset-Proof; $script:Firewall=$script:Firewall.Replace('research-bootstrap-isolation','other-policy'); Throws { Get-VerifiedResearchRejectCounter $script:Gate } '*policy firewall jump missing*' }
Test 'missing terminal REJECT rule rejected' { Reset-Proof; $script:Firewall=$script:Firewall.Replace('-j REJECT --reject-with icmp-port-unreachable','-j DROP'); Throws { Get-VerifiedResearchRejectCounter $script:Gate } '*reject rule absent*' }
Test 'unavailable counter rejected' { Reset-Proof; $script:CounterError=$true; Throws { Get-VerifiedResearchRejectCounter $script:Gate } '*Cannot inspect firewall packet counter*' }
Test 'ambiguous counter rejected' { Reset-Proof; $script:DuplicateCounter=$true; Throws { Get-VerifiedResearchRejectCounter $script:Gate } '*Ambiguous reject counter*' }
foreach ($target in @(@{host='10.43.139.131';port=80},@{host='10.42.2.51';port=8000})) {
    Test "verified refusal and increasing counter accepted: $($target.host)" {
        Reset-Proof; Invoke-IsolationProbe reviewed-caller $target.host $target.port -Denied
        $call=@($script:NativeCalls | Where-Object program -eq 'kubectl')[0]
        Assert (($call.arguments[0..8] -join '|') -ceq '--context|k3d-ai-investment-dev|-n|ai-investment|exec|pod/reviewed-caller|--|bash|-c') 'Probe remote argument boundary lost'
    }
}
Test 'timeout cannot establish isolation even with a counter increase' { Reset-Proof; $script:Probe.ExitCode=124; Throws { Invoke-IsolationProbe reviewed-caller 10.42.2.51 8000 -Denied } '*exit=124*counter=7->9*' }
Test 'generic exit one cannot establish isolation' { Reset-Proof; $script:Probe.StdErr='command terminated with exit code 1'; Throws { Invoke-IsolationProbe reviewed-caller 10.42.2.51 8000 -Denied } '*refused=False*' }
Test 'unchanged counter cannot establish isolation' { Reset-Proof; $script:Counters.Clear(); $script:Counters.Enqueue(7); $script:Counters.Enqueue(7); Throws { Invoke-IsolationProbe reviewed-caller 10.42.2.51 8000 -Denied } '*counter=7->7*' }
Test 'counter reset cannot establish isolation' { Reset-Proof; $script:Counters.Clear(); $script:Counters.Enqueue(7); $script:Counters.Enqueue(0); Throws { Invoke-IsolationProbe reviewed-caller 10.42.2.51 8000 -Denied } '*counter=7->0*' }
Test 'unexpected caller rejected before probing' { Reset-Proof; Throws { Invoke-IsolationProbe other-caller 10.42.2.51 8000 -Denied } '*reviewed research service/pod*'; Assert ($script:NativeCalls.Count -eq 0) 'Unexpected probe' }
Test 'unexpected target rejected before probing' { Reset-Proof; Throws { Invoke-IsolationProbe reviewed-caller 10.42.2.99 8000 -Denied } '*reviewed research service/pod*'; Assert ($script:NativeCalls.Count -eq 0) 'Unexpected probe' }
Write-Output "$script:Passed tests passed; no external commands executed."
