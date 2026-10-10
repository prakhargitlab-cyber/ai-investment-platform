# Pester v5 tests for Invoke-SynchronizedDumpWithRowCounts
# (scripts/database/PostgresBackupCommon.psm1), covering the backup-side
# fix for the row-count-vs-dump-snapshot consistency defect.
#
# All Kubernetes/PostgreSQL I/O is mocked via Invoke-PodExec -- no live
# pod, cluster, or database is touched. These tests were written but NOT
# executed in this environment -- no PowerShell interpreter (pwsh/
# powershell) is available anywhere this work was done. Run with:
# Invoke-Pester -Path <this file> on a Windows machine with Pester 5+
# installed before relying on this.

BeforeAll {
    . (Join-Path $PSScriptRoot "..\PostgresBackupCommon.psm1")
}

Describe "Invoke-SynchronizedDumpWithRowCounts" {

    Context "Successful synchronized capture" {
        It "returns exact counts for every enumerated table, including a table with zero rows" {
            Mock Invoke-PodExec {
                if ($Arguments -contains "information_schema.tables") {
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
            Mock Invoke-PodExec {
                if ($Arguments -contains "information_schema.tables") {
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
            Mock Invoke-PodExec {
                if ($Arguments -contains "information_schema.tables") {
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
            Mock Invoke-PodExec {
                if ($Arguments -contains "information_schema.tables") {
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
            Mock Invoke-PodExec {
                return [PSCustomObject]@{ ExitCode = 0; Output = "public|accounts; drop table other" }
            }

            { Invoke-SynchronizedDumpWithRowCounts -Namespace "ai-investment" -PodName "postgres-0" `
                -DatabaseUser "investment" -DatabaseName "investment" -RemoteDumpPath "/tmp/x.dump" -BackupId "2026-10-10_120000_abcdef" } |
                Should -Throw "*is not a safe identifier*"
        }
    }

    Context "No application tables found" {
        It "throws rather than producing a backup with no verifiable row-count snapshot" {
            Mock Invoke-PodExec {
                return [PSCustomObject]@{ ExitCode = 0; Output = "" }
            }

            { Invoke-SynchronizedDumpWithRowCounts -Namespace "ai-investment" -PodName "postgres-0" `
                -DatabaseUser "investment" -DatabaseName "investment" -RemoteDumpPath "/tmp/x.dump" -BackupId "2026-10-10_120000_abcdef" } |
                Should -Throw "*No application tables were found*"
        }
    }
}

Describe "Restore-side row-count trust gating (tableRowCountsCaptureMethod)" {
    # restore.ps1's comparison logic is embedded inline in the large
    # Invoke-PlatformRestore function (it depends on kubectl/psql state
    # established earlier in that same function), so it is exercised here
    # at the level of the gating condition itself -- the same expression
    # restore.ps1 uses to decide whether a manifest's recorded row counts
    # are trustworthy enough to compare against.
    It "does not trust a manifest with tableRowCounts but no capture method (pre-Stage-2B backup)" {
        $manifestDb = [PSCustomObject]@{ tableRowCounts = [PSCustomObject]@{ "public.accounts" = 42 }; tableRowCountsCaptureMethod = $null }
        $trustworthy = ($manifestDb.tableRowCounts -and $manifestDb.tableRowCountsCaptureMethod -eq "SYNCHRONIZED_PG_EXPORT_SNAPSHOT")
        $trustworthy | Should -BeFalse
    }

    It "does not trust a manifest whose capture method is an unrecognized/older value" {
        $manifestDb = [PSCustomObject]@{ tableRowCounts = [PSCustomObject]@{ "public.accounts" = 42 }; tableRowCountsCaptureMethod = "LIVE_QUERY_AFTER_DUMP" }
        $trustworthy = ($manifestDb.tableRowCounts -and $manifestDb.tableRowCountsCaptureMethod -eq "SYNCHRONIZED_PG_EXPORT_SNAPSHOT")
        $trustworthy | Should -BeFalse
    }

    It "trusts a manifest whose capture method is the synchronized snapshot method" {
        $manifestDb = [PSCustomObject]@{ tableRowCounts = [PSCustomObject]@{ "public.accounts" = 42 }; tableRowCountsCaptureMethod = "SYNCHRONIZED_PG_EXPORT_SNAPSHOT" }
        $trustworthy = ($manifestDb.tableRowCounts -and $manifestDb.tableRowCountsCaptureMethod -eq "SYNCHRONIZED_PG_EXPORT_SNAPSHOT")
        $trustworthy | Should -BeTrue
    }

    It "does not trust a manifest with no tableRowCounts at all (pre-Stage-2 backup)" {
        $manifestDb = [PSCustomObject]@{ tableRowCounts = $null; tableRowCountsCaptureMethod = $null }
        $trustworthy = ($manifestDb.tableRowCounts -and $manifestDb.tableRowCountsCaptureMethod -eq "SYNCHRONIZED_PG_EXPORT_SNAPSHOT")
        $trustworthy | Should -BeFalse
    }
}
