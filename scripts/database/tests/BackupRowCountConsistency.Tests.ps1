# Pester v5 tests for Invoke-SynchronizedDumpWithRowCounts
# (scripts/database/PostgresBackupCommon.psm1), covering the backup-side
# fix for the row-count-vs-dump-snapshot consistency defect.
#
# Kubernetes/PostgreSQL I/O is mocked in the production module scope.

BeforeAll {
    Import-Module (Join-Path $PSScriptRoot "..\PostgresBackupCommon.psm1") -Force
}

Describe "Invoke-SynchronizedDumpWithRowCounts" {

    Context "Successful synchronized capture" {
        It "returns exact counts for every enumerated table, including a table with zero rows" {
            Mock Invoke-PodExec -ModuleName PostgresBackupCommon {
                if ($Arguments[0] -eq "psql" -and ($Arguments -join " ") -match "information_schema.tables") {
                    return [PSCustomObject]@{ ExitCode = 0; Output = "public|accounts`npublic|empty_table" }
                }
                # The combined psql+pg_dump+count script, run via sh -c.
                return [PSCustomObject]@{ ExitCode = 0; Output = "public.accounts|42`npublic.empty_table|0" }
            }

            $result = Invoke-SynchronizedDumpWithRowCounts -Namespace "ai-investment" -PodName "postgres-0" `
                -DatabaseUser "investment" -DatabaseName "investment" -RemoteDumpPath "/tmp/x.dump" -BackupId "2026-10-10_120000_abcdef"

            $result["public.accounts"] | Should -Be 42
            $result["public.empty_table"] | Should -Be 0
            $result.Count | Should -Be 2
        }
    }

    Context "pg_dump failure inside the synchronized script (concurrent-write / disk-full style failure)" {
        It "throws rather than returning counts, when the combined script's overall exit code is non-zero" {
            Mock Invoke-PodExec -ModuleName PostgresBackupCommon {
                if ($Arguments[0] -eq "psql" -and ($Arguments -join " ") -match "information_schema.tables") {
                    return [PSCustomObject]@{ ExitCode = 0; Output = "public|accounts" }
                }
                # test -f <okflag> failed because pg_dump itself failed and
                # never touched the ok-flag -- the overall sh -c exits non-zero.
                return [PSCustomObject]@{ ExitCode = 1; Output = "pg_dump: error: out of disk space" }
            }

            { Invoke-SynchronizedDumpWithRowCounts -Namespace "ai-investment" -PodName "postgres-0" `
                -DatabaseUser "investment" -DatabaseName "investment" -RemoteDumpPath "/tmp/x.dump" -BackupId "2026-10-10_120000_abcdef" } |
                Should -Throw "*Synchronized backup capture*failed*"
        }
    }

    Context "Malformed / incomplete row-count output (never silently skipped)" {
        It "throws when a table enumerated for counting is missing from the captured output" {
            Mock Invoke-PodExec -ModuleName PostgresBackupCommon {
                if ($Arguments[0] -eq "psql" -and ($Arguments -join " ") -match "information_schema.tables") {
                    return [PSCustomObject]@{ ExitCode = 0; Output = "public|accounts`npublic|orders" }
                }
                # Only one of the two enumerated tables came back.
                return [PSCustomObject]@{ ExitCode = 0; Output = "public.accounts|42" }
            }

            { Invoke-SynchronizedDumpWithRowCounts -Namespace "ai-investment" -PodName "postgres-0" `
                -DatabaseUser "investment" -DatabaseName "investment" -RemoteDumpPath "/tmp/x.dump" -BackupId "2026-10-10_120000_abcdef" } |
                Should -Throw "*did not report a count for table*"
        }

        It "throws on an output line with a non-numeric count instead of silently dropping it" {
            Mock Invoke-PodExec -ModuleName PostgresBackupCommon {
                if ($Arguments[0] -eq "psql" -and ($Arguments -join " ") -match "information_schema.tables") {
                    return [PSCustomObject]@{ ExitCode = 0; Output = "public|accounts" }
                }
                return [PSCustomObject]@{ ExitCode = 0; Output = "public.accounts|not-a-number" }
            }

            { Invoke-SynchronizedDumpWithRowCounts -Namespace "ai-investment" -PodName "postgres-0" `
                -DatabaseUser "investment" -DatabaseName "investment" -RemoteDumpPath "/tmp/x.dump" -BackupId "2026-10-10_120000_abcdef" } |
                Should -Throw "*non-numeric count*"
        }
    }

    Context "Unsafe/unsupported table identifiers (never silently skipped)" {
        It "throws instead of silently omitting a table whose name fails identifier validation" {
            Mock Invoke-PodExec -ModuleName PostgresBackupCommon {
                return [PSCustomObject]@{ ExitCode = 0; Output = "public|accounts; drop table other" }
            }

            { Invoke-SynchronizedDumpWithRowCounts -Namespace "ai-investment" -PodName "postgres-0" `
                -DatabaseUser "investment" -DatabaseName "investment" -RemoteDumpPath "/tmp/x.dump" -BackupId "2026-10-10_120000_abcdef" } |
                Should -Throw "*is not a safe identifier*"
        }
    }

    Context "No application tables found" {
        It "throws rather than producing a backup with no verifiable row-count snapshot" {
            Mock Invoke-PodExec -ModuleName PostgresBackupCommon {
                return [PSCustomObject]@{ ExitCode = 0; Output = "" }
            }

            { Invoke-SynchronizedDumpWithRowCounts -Namespace "ai-investment" -PodName "postgres-0" `
                -DatabaseUser "investment" -DatabaseName "investment" -RemoteDumpPath "/tmp/x.dump" -BackupId "2026-10-10_120000_abcdef" } |
                Should -Throw "*No application tables were found*"
        }
    }
}

Describe "Restore-side row-count trust gating (tableRowCountsCaptureMethod)" {
    It "is called by the real restore function" {
        $tokens = $null; $errors = $null
        $ast = [Management.Automation.Language.Parser]::ParseFile((Join-Path $PSScriptRoot '..\restore.ps1'), [ref]$tokens, [ref]$errors)
        $errors.Count | Should -Be 0
        $calls = @($ast.FindAll({ param($node)
            $node -is [Management.Automation.Language.CommandAst] -and $node.GetCommandName() -eq 'Resolve-RowCountsTrustworthy'
        }, $true))
        $calls.Count | Should -Be 1
    }

    It "rejects empty or malformed synchronized manifests" {
        foreach ($counts in @([pscustomobject]@{}, [pscustomobject]@{'public.accounts'=-1}, [pscustomobject]@{'public.accounts'='garbage'})) {
            Resolve-RowCountsTrustworthy -ManifestDatabase ([pscustomobject]@{
                tableRowCounts=$counts; tableRowCountsCaptureMethod='SYNCHRONIZED_PG_EXPORT_SNAPSHOT'
            }) | Should -BeFalse
        }
    }
    # Exercise the exported helper called by the real restore command.
    It "does not trust a manifest with tableRowCounts but no capture method (pre-Stage-2B backup)" {
        $manifestDb = [PSCustomObject]@{ tableRowCounts = [PSCustomObject]@{ "public.accounts" = 42 }; tableRowCountsCaptureMethod = $null }
        $trustworthy = (Resolve-RowCountsTrustworthy -ManifestDatabase $manifestDb)
        $trustworthy | Should -BeFalse
    }

    It "does not trust a manifest whose capture method is an unrecognized/older value" {
        $manifestDb = [PSCustomObject]@{ tableRowCounts = [PSCustomObject]@{ "public.accounts" = 42 }; tableRowCountsCaptureMethod = "LIVE_QUERY_AFTER_DUMP" }
        $trustworthy = (Resolve-RowCountsTrustworthy -ManifestDatabase $manifestDb)
        $trustworthy | Should -BeFalse
    }

    It "trusts a manifest whose capture method is the synchronized snapshot method" {
        $manifestDb = [PSCustomObject]@{ tableRowCounts = [PSCustomObject]@{ "public.accounts" = 42 }; tableRowCountsCaptureMethod = "SYNCHRONIZED_PG_EXPORT_SNAPSHOT" }
        $trustworthy = (Resolve-RowCountsTrustworthy -ManifestDatabase $manifestDb)
        $trustworthy | Should -BeTrue
    }

    It "does not trust a manifest with no tableRowCounts at all (pre-Stage-2 backup)" {
        $manifestDb = [PSCustomObject]@{ tableRowCounts = $null; tableRowCountsCaptureMethod = $null }
        $trustworthy = (Resolve-RowCountsTrustworthy -ManifestDatabase $manifestDb)
        $trustworthy | Should -BeFalse
    }
}

Describe 'Durable provisioning marker claim' {
    It 'accepts exactly one successful claim' {
        Mock Invoke-PodExec -ModuleName PostgresBackupCommon { [pscustomobject]@{ExitCode=0; Output="claimed`n"} }
        Use-FreshProvisioningMarker -Namespace isolated -PodName fixture -DatabaseUser tester -DatabaseName fixture | Should -BeTrue
        Should -Invoke Invoke-PodExec -ModuleName PostgresBackupCommon -Times 1 -Exactly -ParameterFilter {
            $Arguments -contains 'postgres' -and ($Arguments -join ' ') -match 'UPDATE platform_bootstrap.provisioning'
        }
    }
    It 'fails closed on query errors and unexpected or absent results' {
        foreach ($response in @(
            [pscustomobject]@{ExitCode=1; Output='claimed'},
            [pscustomobject]@{ExitCode=0; Output=''},
            [pscustomobject]@{ExitCode=0; Output="claimed`nclaimed"}
        )) {
            Mock Invoke-PodExec -ModuleName PostgresBackupCommon { $response }
            Use-FreshProvisioningMarker -Namespace isolated -PodName fixture -DatabaseUser tester -DatabaseName fixture | Should -BeFalse
        }
    }
}

Describe 'Durable restore record for automatic ADMIN denial' {
    It 'allows the first-install path when no restore ledger exists' {
        Mock Invoke-PodExec -ModuleName PostgresBackupCommon { [pscustomobject]@{ExitCode=0; Output='f'} }
        Test-DatabaseRestoreRecorded -Namespace isolated -PodName fixture -DatabaseUser tester -DatabaseName fixture | Should -BeFalse
    }
    It 'detects a restored database' {
        Mock Invoke-PodExec -ModuleName PostgresBackupCommon { [pscustomobject]@{ExitCode=0; Output='t'} }
        Test-DatabaseRestoreRecorded -Namespace isolated -PodName fixture -DatabaseUser tester -DatabaseName fixture | Should -BeTrue
        Should -Invoke Invoke-PodExec -ModuleName PostgresBackupCommon -Times 2 -Exactly
    }
    It 'fails closed on a query error' {
        Mock Invoke-PodExec -ModuleName PostgresBackupCommon { [pscustomobject]@{ExitCode=1; Output=''} }
        { Test-DatabaseRestoreRecorded -Namespace isolated -PodName fixture -DatabaseUser tester -DatabaseName fixture } | Should -Throw '*automatic bootstrap denied*'
    }
    It 'fails closed on malformed output' {
        Mock Invoke-PodExec -ModuleName PostgresBackupCommon { [pscustomobject]@{ExitCode=0; Output='unknown'} }
        { Test-DatabaseRestoreRecorded -Namespace isolated -PodName fixture -DatabaseUser tester -DatabaseName fixture } | Should -Throw '*automatic bootstrap denied*'
    }
}
