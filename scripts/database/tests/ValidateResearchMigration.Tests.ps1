# Pester 5 safety tests for scripts/database/validate-research-migration.ps1.
#
# Covers the identity-verification and Maven-delegation redesign: this
# script no longer replays migration SQL itself -- it independently verifies
# instance identity (system_identifier, never a URL/name/port), creates a
# disposable database, and delegates ALL migration/fixture/constraint
# verification to `mvn` running the real ResearchFlywayMigrationTest. Every
# external boundary this script can reach -- psql (candidate target),
# kubectl exec (known main/ETF-test instances), and mvn (the Java test) --
# is replaced with a Pester Mock in BeforeEach, before any It block runs.
# No test in this file can reach a real psql/kubectl/mvn process, a real
# network connection, a real database, or Kubernetes.

Describe "validate-research-migration" {
    BeforeAll {
        $script:RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..\..")).Path
        $script:ScriptUnderTest = Join-Path $script:RepoRoot "scripts\database\validate-research-migration.ps1"
        if (-not (Test-Path -LiteralPath $script:ScriptUnderTest)) {
            throw "Validation script not found: $script:ScriptUnderTest"
        }
        . $script:ScriptUnderTest
        Import-Module (Join-Path $script:RepoRoot "scripts\database\PostgresBackupCommon.psm1") -Force

        # Every mocked call is appended here so assertions can inspect
        # exactly what was issued, in order, without ever running it for real.
        $script:Calls = @()
    }

    BeforeEach {
        $script:Calls = @()
        $script:EtfOnboardInsertCount = 0
        $script:probeCount = 0

        # Known-instance identity probes: distinct, non-colliding values by
        # default. A test that wants a collision overrides this mock.
        Mock Invoke-ResearchMigrationKubectlExec {
            param($Pod, $Database, $User, $Sql)
            $script:Calls += [pscustomobject]@{ Boundary = "kubectl"; Pod = $Pod; Sql = $Sql }
            if ($Pod -eq "postgres-0") { return @{ ExitCode = 0; Output = "1111111111111111111" } }
            return @{ ExitCode = 0; Output = "2222222222222222222" }
        }

        # Candidate (disposable) target: a distinct identity by default, so
        # it never accidentally collides with either known instance above.
        Mock Invoke-ResearchMigrationPsql {
            param($ConnectionInfo, [string]$Database, [string]$Sql)
            $script:Calls += [pscustomobject]@{ Boundary = "psql"; Database = $Database; Sql = $Sql }
            if ($Sql -match "system_identifier") { return @{ ExitCode = 0; Output = "3333333333333333333" } }
            if ($Sql -match "pg_database") { return @{ ExitCode = 0; Output = "" } }
            return @{ ExitCode = 0; Output = "" }
        }

        # Maven/Java delegation: default to a genuine, successful run.
        Mock Invoke-ResearchMigrationMaven {
            param($ProjectRoot, [string[]]$Arguments)
            $script:Calls += [pscustomobject]@{ Boundary = "mvn"; Arguments = ($Arguments -join " ") }
            return @{ ExitCode = 0; Output = "Tests run: 1, Failures: 0, Errors: 0, Skipped: 0" }
        }
    }

    Context "dry run (default behavior)" {
        It "issues no SQL, kubectl or mvn call at all when -AuthorizeMigrationValidation is absent" {
            Invoke-PlatformValidateResearchMigration -ProjectRoot $script:RepoRoot | Out-Null
            $script:Calls.Count | Should -Be 0
        }
    }

    Context "authorization and target validation fail closed" {
        It "throws when -AuthorizeMigrationValidation is passed without a DSN, before any call" {
            { Invoke-PlatformValidateResearchMigration -ProjectRoot $script:RepoRoot -AuthorizeMigrationValidation } |
                Should -Throw "*requires -MigrationValidationDsn*"
            $script:Calls.Count | Should -Be 0
        }

        It "rejects the live application database name before any call" {
            { Invoke-PlatformValidateResearchMigration -ProjectRoot $script:RepoRoot -AuthorizeMigrationValidation `
                -MigrationValidationDsn "postgresql://user:pw@127.0.0.1:5432/investment" } | Should -Throw "*reserved database*"
            $script:Calls.Count | Should -Be 0
        }

        It "rejects the existing ETF regression-test database name before any call" {
            { Invoke-PlatformValidateResearchMigration -ProjectRoot $script:RepoRoot -AuthorizeMigrationValidation `
                -MigrationValidationDsn "postgresql://user:pw@127.0.0.1:5432/investment_etf_clean" } | Should -Throw "*reserved database*"
            $script:Calls.Count | Should -Be 0
        }

        It "rejects the Python integration suite's own database naming convention before any call" {
            { Invoke-PlatformValidateResearchMigration -ProjectRoot $script:RepoRoot -AuthorizeMigrationValidation `
                -MigrationValidationDsn "postgresql://user:pw@127.0.0.1:5432/aip_research_test_abc1234567" } |
                Should -Throw "*Python integration suite*"
            $script:Calls.Count | Should -Be 0
        }
    }

    Context "instance-identity verification (system_identifier, never a URL/name/port)" {
        It "aborts before CREATE DATABASE when the candidate matches the main instance's system_identifier" {
            Mock Invoke-ResearchMigrationPsql {
                param($ConnectionInfo, [string]$Database, [string]$Sql)
                $script:Calls += [pscustomobject]@{ Boundary = "psql"; Database = $Database; Sql = $Sql }
                if ($Sql -match "system_identifier") { return @{ ExitCode = 0; Output = "1111111111111111111" } }  # == MAIN_DB
                return @{ ExitCode = 0; Output = "" }
            }
            { Invoke-PlatformValidateResearchMigration -ProjectRoot $script:RepoRoot -AuthorizeMigrationValidation `
                -MigrationValidationDsn "postgresql://user:pw@127.0.0.1:5432/disposable_migration_admin" } |
                Should -Throw "*SAME PostgreSQL instance as the main application database*"
            @($script:Calls | Where-Object { $_.Sql -match "create database" }).Count | Should -Be 0
            @($script:Calls | Where-Object { $_.Boundary -eq "mvn" }).Count | Should -Be 0
        }

        It "aborts before CREATE DATABASE when the candidate matches the ETF-test instance's system_identifier" {
            Mock Invoke-ResearchMigrationPsql {
                param($ConnectionInfo, [string]$Database, [string]$Sql)
                $script:Calls += [pscustomobject]@{ Boundary = "psql"; Database = $Database; Sql = $Sql }
                if ($Sql -match "system_identifier") { return @{ ExitCode = 0; Output = "2222222222222222222" } }  # == ETF_TEST_DB
                return @{ ExitCode = 0; Output = "" }
            }
            { Invoke-PlatformValidateResearchMigration -ProjectRoot $script:RepoRoot -AuthorizeMigrationValidation `
                -MigrationValidationDsn "postgresql://user:pw@127.0.0.1:5432/disposable_migration_admin" } |
                Should -Throw "*SAME PostgreSQL instance as the existing ETF test database*"
            @($script:Calls | Where-Object { $_.Sql -match "create database" }).Count | Should -Be 0
        }

        It "fails closed (does not guess) when the known-instance identity probe cannot complete" {
            Mock Invoke-ResearchMigrationKubectlExec {
                param($Pod, $Database, $User, $Sql)
                $script:Calls += [pscustomobject]@{ Boundary = "kubectl"; Pod = $Pod; Sql = $Sql }
                return @{ ExitCode = 1; Output = "" }
            }
            { Invoke-PlatformValidateResearchMigration -ProjectRoot $script:RepoRoot -AuthorizeMigrationValidation `
                -MigrationValidationDsn "postgresql://user:pw@127.0.0.1:5432/disposable_migration_admin" } |
                Should -Throw "*Unable to independently verify*"
            @($script:Calls | Where-Object { $_.Boundary -eq "mvn" }).Count | Should -Be 0
        }

        It "fails closed when the candidate's own identity probe cannot complete" {
            Mock Invoke-ResearchMigrationPsql {
                param($ConnectionInfo, [string]$Database, [string]$Sql)
                $script:Calls += [pscustomobject]@{ Boundary = "psql"; Database = $Database; Sql = $Sql }
                if ($Sql -match "system_identifier") { return @{ ExitCode = 1; Output = "" } }
                return @{ ExitCode = 0; Output = "" }
            }
            { Invoke-PlatformValidateResearchMigration -ProjectRoot $script:RepoRoot -AuthorizeMigrationValidation `
                -MigrationValidationDsn "postgresql://user:pw@127.0.0.1:5432/disposable_migration_admin" } |
                Should -Throw "*Unable to determine the candidate disposable instance's system_identifier*"
            @($script:Calls | Where-Object { $_.Sql -match "create database" }).Count | Should -Be 0
        }
    }

    Context "disposable-database identity" {
        It "aborts before CREATE DATABASE when the generated name already exists" {
            Mock Invoke-ResearchMigrationPsql {
                param($ConnectionInfo, [string]$Database, [string]$Sql)
                $script:Calls += [pscustomobject]@{ Boundary = "psql"; Database = $Database; Sql = $Sql }
                if ($Sql -match "system_identifier") { return @{ ExitCode = 0; Output = "3333333333333333333" } }
                if ($Sql -match "pg_database") { return @{ ExitCode = 0; Output = "1" } }  # "already exists"
                return @{ ExitCode = 0; Output = "" }
            }
            { Invoke-PlatformValidateResearchMigration -ProjectRoot $script:RepoRoot -AuthorizeMigrationValidation `
                -MigrationValidationDsn "postgresql://user:pw@127.0.0.1:5432/disposable_migration_admin" } | Should -Throw "*already exists*"
            @($script:Calls | Where-Object { $_.Sql -match "create database" }).Count | Should -Be 0
        }
    }

    Context "Maven/Java delegation and skipped-test detection" {
        It "passes the module-scoped Maven invocation with the independently-verified identity values" {
            Invoke-PlatformValidateResearchMigration -ProjectRoot $script:RepoRoot -AuthorizeMigrationValidation `
                -MigrationValidationDsn "postgresql://user:pw@127.0.0.1:5432/disposable_migration_admin" | Out-Null
            $mvnCalls = @($script:Calls | Where-Object { $_.Boundary -eq "mvn" })
            $mvnCalls.Count | Should -Be 1
            $mvnCalls[0].Arguments | Should -Match ([regex]::Escape("-pl services/research-service"))
            $mvnCalls[0].Arguments | Should -Not -Match ([regex]::Escape("-am"))
            $mvnCalls[0].Arguments | Should -Match ([regex]::Escape("-Dtest=ResearchFlywayEtfOnboardMigrationTest#etfOnboardMigrationUpgradesAndPreservesCheckpoints"))
            $mvnCalls[0].Arguments | Should -Match ([regex]::Escape("-DfailIfNoTests=true"))
            $mvnCalls[0].Arguments | Should -Match "-DetfOnboardMigrationApprovedSystemIdentifier=3333333333333333333"
            $mvnCalls[0].Arguments | Should -Match "-DetfOnboardMigrationForbiddenSystemIdentifiers=1111111111111111111,2222222222222222222"
        }

        It "throws when mvn exits nonzero, and never attempts to drop the database" {
            Mock Invoke-ResearchMigrationMaven {
                param($ProjectRoot, [string[]]$Arguments)
                $script:Calls += [pscustomobject]@{ Boundary = "mvn"; Arguments = ($Arguments -join " ") }
                return @{ ExitCode = 1; Output = "Tests run: 1, Failures: 1, Errors: 0, Skipped: 0" }
            }
            { Invoke-PlatformValidateResearchMigration -ProjectRoot $script:RepoRoot -AuthorizeMigrationValidation `
                -MigrationValidationDsn "postgresql://user:pw@127.0.0.1:5432/disposable_migration_admin" } | Should -Throw "*FAILED*preserved for inspection*"
            @($script:Calls | Where-Object { $_.Sql -match "drop database" }).Count | Should -Be 0
        }

        It "treats a Maven success with a SKIPPED test as inconclusive, not as migration-compatibility evidence" {
            Mock Invoke-ResearchMigrationMaven {
                param($ProjectRoot, [string[]]$Arguments)
                $script:Calls += [pscustomobject]@{ Boundary = "mvn"; Arguments = ($Arguments -join " ") }
                return @{ ExitCode = 0; Output = "Tests run: 1, Failures: 0, Errors: 0, Skipped: 1" }
            }
            { Invoke-PlatformValidateResearchMigration -ProjectRoot $script:RepoRoot -AuthorizeMigrationValidation `
                -MigrationValidationDsn "postgresql://user:pw@127.0.0.1:5432/disposable_migration_admin" } | Should -Throw "*SKIPPED*"
        }

        It "succeeds, and does NOT drop the database, when mvn genuinely ran and passed" {
            Invoke-PlatformValidateResearchMigration -ProjectRoot $script:RepoRoot -AuthorizeMigrationValidation `
                -MigrationValidationDsn "postgresql://user:pw@127.0.0.1:5432/disposable_migration_admin" | Out-Null
            @($script:Calls | Where-Object { $_.Sql -match "drop database" }).Count | Should -Be 0
            @($script:Calls | Where-Object { $_.Sql -match "create database" }).Count | Should -Be 1
        }
    }

    Context "Test-MavenSurefireActuallyRan" {
        It "returns true only for a genuine run with zero skips" {
            Test-MavenSurefireActuallyRan -Output "Tests run: 1, Failures: 0, Errors: 0, Skipped: 0" | Should -Be $true
        }
        It "returns false for a skipped test, a failure, or unparsable output" {
            Test-MavenSurefireActuallyRan -Output "Tests run: 1, Failures: 0, Errors: 0, Skipped: 1" | Should -Be $false
            Test-MavenSurefireActuallyRan -Output "Tests run: 1, Failures: 1, Errors: 0, Skipped: 0" | Should -Be $false
            Test-MavenSurefireActuallyRan -Output "BUILD FAILURE: could not resolve dependencies" | Should -Be $false
        }
    }

    Context "cleanup is a wholly separate, separately-authorized action" {
        It "throws when -AuthorizeMigrationCleanup is used without -CleanupDatabaseName" {
            { Invoke-PlatformValidateResearchMigration -ProjectRoot $script:RepoRoot -AuthorizeMigrationCleanup `
                -MigrationValidationDsn "postgresql://user:pw@127.0.0.1:5432/disposable_migration_admin" } | Should -Throw "*requires -CleanupDatabaseName*"
            $script:Calls.Count | Should -Be 0
        }

        It "rejects a database name that does not match this command's own generated-name convention" {
            { Invoke-PlatformValidateResearchMigration -ProjectRoot $script:RepoRoot -AuthorizeMigrationCleanup `
                -MigrationValidationDsn "postgresql://user:pw@127.0.0.1:5432/disposable_migration_admin" `
                -CleanupDatabaseName "investment" } | Should -Throw "*own naming convention*"
            $script:Calls.Count | Should -Be 0
        }

        It "refuses cleanup when the named database does not exist" {
            Mock Invoke-ResearchMigrationPsql {
                param($ConnectionInfo, [string]$Database, [string]$Sql)
                $script:Calls += [pscustomobject]@{ Boundary = "psql"; Database = $Database; Sql = $Sql }
                return @{ ExitCode = 0; Output = "" }   # "does not exist"
            }
            { Invoke-PlatformValidateResearchMigration -ProjectRoot $script:RepoRoot -AuthorizeMigrationCleanup `
                -MigrationValidationDsn "postgresql://user:pw@127.0.0.1:5432/disposable_migration_admin" `
                -CleanupDatabaseName "aip_migcheck_abc1234567" } | Should -Throw "*does not exist*"
            @($script:Calls | Where-Object { $_.Sql -match "drop database" }).Count | Should -Be 0
        }

        It "drops exactly the named, confirmed-existing database and nothing else" {
            Mock Invoke-ResearchMigrationPsql {
                param($ConnectionInfo, [string]$Database, [string]$Sql)
                $script:Calls += [pscustomobject]@{ Boundary = "psql"; Database = $Database; Sql = $Sql }
                if ($Sql -match "pg_database") { return @{ ExitCode = 0; Output = "1" } }
                return @{ ExitCode = 0; Output = "" }
            }
            Invoke-PlatformValidateResearchMigration -ProjectRoot $script:RepoRoot -AuthorizeMigrationCleanup `
                -MigrationValidationDsn "postgresql://user:pw@127.0.0.1:5432/disposable_migration_admin" `
                -CleanupDatabaseName "aip_migcheck_abc1234567" | Out-Null
            $dropCalls = @($script:Calls | Where-Object { $_.Sql -match "drop database" })
            $dropCalls.Count | Should -Be 1
            $dropCalls[0].Sql | Should -Match "aip_migcheck_abc1234567"
        }

        It "rejects cleanup against a reserved connection target" {
            { Invoke-PlatformValidateResearchMigration -ProjectRoot $script:RepoRoot -AuthorizeMigrationCleanup `
                -MigrationValidationDsn "postgresql://user:pw@127.0.0.1:5432/investment" `
                -CleanupDatabaseName "aip_migcheck_abc1234567" } | Should -Throw "*reserved database*"
            $script:Calls.Count | Should -Be 0
        }
    }

    Context "disposable-name validation" {
        It "rejects anything not matching this command's own generated-name convention" {
            { Test-SafeMigrationDatabaseName -Name "aip_research_test_abc1234567" } | Should -Throw "*own naming convention*"
            { Test-SafeMigrationDatabaseName -Name "investment" } | Should -Throw "*own naming convention*"
        }
        It "accepts a correctly shaped generated name" {
            { Test-SafeMigrationDatabaseName -Name "aip_migcheck_abc1234567" } | Should -Not -Throw
        }
    }

    Context "migration file discovery (no second copy of the migration path)" {
        It "finds V24 under the real `$ResearchMigrationDir the production script defines" {
            $files = Get-ResearchMigrationFiles -ProjectRoot $script:RepoRoot
            @($files | Where-Object { $_.Version -eq 24 }).Count | Should -Be 1
        }
        It "orders migration files by version" {
            $files = Get-ResearchMigrationFiles -ProjectRoot $script:RepoRoot
            $versions = $files | ForEach-Object { $_.Version }
            ($versions | Sort-Object) -join "," | Should -Be ($versions -join ",")
        }
        It "fails closed with a clear error when V24 is missing from the checkout" {
            $empty = Join-Path ([IO.Path]::GetTempPath()) ([guid]::NewGuid().ToString("N"))
            New-Item -ItemType Directory -Path (Join-Path $empty "services\research-service\src\main\resources\db\migration") -Force | Out-Null
            "select 1;" | Set-Content (Join-Path $empty "services\research-service\src\main\resources\db\migration\V1__x.sql")
            try {
                { Get-MigrationValidationPlan -ProjectRoot $empty } | Should -Throw "*V24 migration not found*"
            } finally {
                Remove-Item -Recurse -Force $empty
            }
        }
        It "fails closed with a clear error when no migration files exist at all" {
            $empty = Join-Path ([IO.Path]::GetTempPath()) ([guid]::NewGuid().ToString("N"))
            New-Item -ItemType Directory -Path (Join-Path $empty "services\research-service\src\main\resources\db\migration") -Force | Out-Null
            try {
                { Get-ResearchMigrationFiles -ProjectRoot $empty } | Should -Throw "*No Flyway migration files found*"
            } finally {
                Remove-Item -Recurse -Force $empty
            }
        }
    }
}
