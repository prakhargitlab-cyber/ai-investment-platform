# On-demand, review-only Flyway V23 -> V24 migration validation for the
# research-service `global_opportunity_cycle_progress.phase` constraint
# (adds ETF_ONBOARD). Modular script under scripts/database/, dot-sourced
# from platform.ps1 exactly like backup.ps1/restore.ps1; never auto-invoked.
#
# Safety model (mirrors restore.ps1's -AuthorizeDestructiveRestore pattern):
#   - Dry-run is the ONLY behavior reachable without an explicit switch: it
#     never opens a database connection, never runs SQL, never creates or
#     drops anything. It only inspects local migration files and prints the
#     plan the authorized run would follow.
#   - The authorized/mutating path additionally requires an operator-supplied
#     connection string to a server the operator has set aside as disposable
#     (the same contract as the Python suite's AIP_TEST_POSTGRES_ADMIN_DSN).
#     It is rejected outright if it resolves to any name this script already
#     knows is a real application database, or to the Python test suite's own
#     naming convention -- this command never reuses or depends on a database
#     any other process created.
#   - Name is not identity: before anything else, this script independently
#     queries the REAL system_identifier of the main and ETF-test PostgreSQL
#     instances (via a read-only kubectl exec probe, never derived from the
#     candidate DSN) and the candidate's own system_identifier, and refuses
#     to proceed if the candidate matches either known instance -- a port-
#     forward or alias to the same cluster is not a disposable instance,
#     whatever its database name or port claims.
#   - The disposable database this run creates gets its own distinct,
#     unambiguous name (prefix "aip_migcheck_", never the Python suite's own
#     "aip_research_test_" prefix) generated once, up front, and held in a
#     single local variable for the remainder of the run.
#   - Actual migration/fixture/constraint verification is delegated entirely
#     to the real Flyway Java API via the existing
#     ResearchFlywayEtfOnboardMigrationTest#etfOnboardMigrationUpgradesAndPreservesCheckpoints
#     JUnit test (services/research-service), invoked through `mvn`, scoped
#     to that one module so a reactor module with no tests (shared-domain/
#     shared-web) never trips -DfailIfNoTests=true. This script never
#     reimplements migration/constraint logic in raw SQL -- it verifies
#     identity and owns disposable-database lifecycle only. The same
#     independently-verified identity values are passed to that test as
#     system properties, so it re-derives and re-checks them itself rather
#     than trusting this script's verdict -- there is no bare-URL path into
#     the Java test that skips identity verification.
#   - On BOTH success and failure, the disposable database is preserved for
#     evidence review -- it is never auto-dropped. Cleanup is a wholly
#     separate, separately-authorized action (-AuthorizeMigrationCleanup).
#   - This script never invokes the Python integration suite
#     (test_postgres_migrations_and_checkpoint.py) or its `database` fixture.
#     That fixture always generates and force-drops its OWN uuid-named
#     database -- never a name this script supplies -- so even the printed,
#     optional, NOT automatically executed suggestion for a follow-up
#     lease-fencing run cannot collide with or drop this run's database.

$ResearchMigrationDir = "services/research-service/src/main/resources/db/migration"

# Names this command must never treat as "the disposable database": the
# live application database, the existing ETF regression-test database, the
# Postgres bootstrap databases, and the Python integration suite's own
# naming convention (so the two tools can never be confused for one another
# or made to collide).
$ReservedResearchMigrationDatabaseNames = @("investment", "investment_etf_clean", "postgres", "template0", "template1")
$ForeignMigrationDatabasePrefix = "aip_research_test_"
$DisposableMigrationDatabasePrefix = "aip_migcheck_"

# The two instances this command must prove a candidate DSN is NOT, by
# real system_identifier rather than name. Mirrors the pod/database/user
# mapping platform.ps1's own Invoke-IsolatedObservationSqlState uses for the
# same two instances -- duplicated here only as two small constant pairs
# (identity-verification plumbing, never migration logic) rather than
# depending on that function directly, since it is gated behind
# -LivePreflight/-IsolatedFrontendGateway flags unrelated to this command.
$KnownPostgresInstances = @{
    MAIN_DB     = @{ Pod = "postgres-0"; Database = "investment"; User = "investment" }
    ETF_TEST_DB = @{ Pod = "postgres-etf-test-0"; Database = "investment_etf_clean"; User = "etf_test" }
}

function Get-ResearchMigrationFiles {
    # Enumerates the real Flyway migration files Research Service owns, in
    # the same version order Flyway itself would apply them. This command
    # never reimplements or hand-copies migration SQL -- it always reads
    # these files directly, so a change to V24 here is validated for what it
    # actually is, not a frozen snapshot of it.
    param([Parameter(Mandatory = $true)][string]$ProjectRoot)
    $dir = Join-Path $ProjectRoot $ResearchMigrationDir
    if (-not (Test-Path $dir)) {
        throw "Research Service migration directory not found: $dir"
    }
    $files = Get-ChildItem -Path $dir -Filter "V*__*.sql" | Where-Object { $_.Name -match '^V(\d+)__' }
    if (-not $files -or $files.Count -eq 0) {
        throw "No Flyway migration files found under $dir"
    }
    $files | ForEach-Object {
        $null = $_.Name -match '^V(\d+)__'
        [pscustomobject]@{ Version = [int]$Matches[1]; Name = $_.Name; Path = $_.FullName }
    } | Sort-Object Version
}

function New-DisposableMigrationDatabaseName {
    # A fresh, unambiguous, collision-resistant name for exactly one run.
    # Never reused across runs and never guessed from a prior run's output.
    $DisposableMigrationDatabasePrefix + ([guid]::NewGuid().ToString("N").Substring(0, 10))
}

function Test-SafeMigrationDatabaseName {
    # Stricter than the shared Test-SafeIdentifier allowlist: requires the
    # exact disposable-database naming convention this command generates,
    # so a typo'd or hand-supplied name can never silently pass as "the"
    # disposable database.
    param([Parameter(Mandatory = $true)][string]$Name)
    Test-SafeIdentifier -Value $Name -FieldName "Disposable migration database name"
    if ($Name -notmatch "^$([regex]::Escape($DisposableMigrationDatabasePrefix))[0-9a-f]{10}$") {
        throw "Refusing to treat '$Name' as the disposable migration-validation database: it does not match this command's own naming convention ($DisposableMigrationDatabasePrefix<10 hex characters>)."
    }
}

function Assert-SafeMigrationValidationTarget {
    # Fails closed on any database identity this command must never touch:
    # the live application database, the existing ETF test database, the
    # Postgres bootstrap databases, and the Python suite's own naming
    # convention. Called before any connection is used for anything other
    # than a read-only existence/identity probe.
    param([Parameter(Mandatory = $true)][string]$DatabaseName)
    if ($ReservedResearchMigrationDatabaseNames -contains $DatabaseName) {
        throw "Refusing to run migration validation against reserved database '$DatabaseName'. Supply a connection string whose database is a disposable server/database set aside for this purpose only."
    }
    if ($DatabaseName -like "$ForeignMigrationDatabasePrefix*") {
        throw "Refusing to run migration validation against '$DatabaseName': that name belongs to the Python integration suite's own convention (AIP_TEST_POSTGRES_ADMIN_DSN), not to this command. Point -MigrationValidationDsn at a disposable server and let this command generate its own database name."
    }
}

function ConvertFrom-PostgresConnectionString {
    # Parses a postgresql://user:pass@host:port/database style DSN into its
    # parts, the same shape the Python suite's own ADMIN_DSN takes. Never
    # logs the password; callers must redact it before printing anything
    # derived from $Raw.
    param([Parameter(Mandatory = $true)][string]$Dsn)
    $uri = $null
    try { $uri = [uri]$Dsn } catch { throw "Unable to parse -MigrationValidationDsn as a connection URI: $($_.Exception.Message)" }
    if ($uri.Scheme -ne "postgresql" -and $uri.Scheme -ne "postgres") {
        throw "Expected a postgresql:// connection string for -MigrationValidationDsn, got scheme '$($uri.Scheme)'."
    }
    $userInfo = $uri.UserInfo -split ":", 2
    $database = $uri.AbsolutePath.TrimStart("/")
    if ([string]::IsNullOrWhiteSpace($database)) { $database = "postgres" }
    [pscustomobject]@{
        Host     = $uri.Host
        Port     = if ($uri.Port -gt 0) { $uri.Port } else { 5432 }
        User     = if ($userInfo.Count -gt 0 -and $userInfo[0]) { $userInfo[0] } else { "postgres" }
        Password = if ($userInfo.Count -gt 1) { $userInfo[1] } else { "" }
        Database = $database
    }
}

function Invoke-ResearchMigrationPsql {
    # The single seam every SQL-touching operation against the CANDIDATE
    # (disposable) target goes through. Kept as its own function so Pester
    # tests can replace it with a mock and assert, without any real network
    # or database, exactly which statements this command would have issued.
    param(
        [Parameter(Mandatory = $true)]$ConnectionInfo,
        [Parameter(Mandatory = $true)][string]$Database,
        [Parameter(Mandatory = $true)][string]$Sql
    )
    Test-SafeIdentifier -Value $Database -FieldName "Target database"
    $env:PGPASSWORD = $ConnectionInfo.Password
    try {
        $output = & psql -h $ConnectionInfo.Host -p $ConnectionInfo.Port -U $ConnectionInfo.User -d $Database -v "ON_ERROR_STOP=1" -At -c $Sql 2>&1
        [pscustomobject]@{ ExitCode = $LASTEXITCODE; Output = ($output -join "`n") }
    } finally {
        Remove-Item Env:\PGPASSWORD -ErrorAction SilentlyContinue
    }
}

function Invoke-ResearchMigrationKubectlExec {
    # The single seam for the read-only kubectl exec probe against a KNOWN
    # (main/ETF-test) instance -- never the disposable candidate, never
    # mutating. Mockable in Pester the same way Invoke-ResearchMigrationPsql
    # and Invoke-ResearchMigrationMaven are.
    param(
        [Parameter(Mandatory = $true)][string]$Pod,
        [Parameter(Mandatory = $true)][string]$Database,
        [Parameter(Mandatory = $true)][string]$User,
        [Parameter(Mandatory = $true)][string]$Sql
    )
    $output = & kubectl --context k3d-ai-investment-dev -n ai-investment exec "pod/$Pod" -c postgres -- `
        psql -U $User -d $Database -At -c $Sql 2>&1
    [pscustomobject]@{ ExitCode = $LASTEXITCODE; Output = ($output -join "`n") }
}

function Get-KnownPostgresSystemIdentifier {
    # Independently queries the REAL system_identifier of one of the two
    # known, already-running instances -- never derived from, or trusting,
    # anything the operator supplied via -MigrationValidationDsn. Fails
    # closed (throws) rather than guessing if the probe cannot complete.
    param([Parameter(Mandatory = $true)][ValidateSet("MAIN_DB", "ETF_TEST_DB")][string]$Alias)
    $target = $KnownPostgresInstances[$Alias]
    $result = Invoke-ResearchMigrationKubectlExec -Pod $target.Pod -Database $target.Database -User $target.User `
        -Sql "select system_identifier from pg_control_system();"
    if ($result.ExitCode -ne 0 -or [string]::IsNullOrWhiteSpace($result.Output)) {
        throw "Unable to independently verify $Alias's system_identifier (required before any migration validation can be authorized): $($result.Output)"
    }
    $result.Output.Trim()
}

function Get-MigrationTargetSystemIdentifier {
    # Queries the candidate disposable target's own system_identifier -- the
    # same value the Java test will independently re-derive and re-check
    # itself, so neither side trusts the other's verdict alone.
    param([Parameter(Mandatory = $true)]$ConnectionInfo)
    $result = Invoke-ResearchMigrationPsql -ConnectionInfo $ConnectionInfo -Database "postgres" `
        -Sql "select system_identifier from pg_control_system();"
    if ($result.ExitCode -ne 0 -or [string]::IsNullOrWhiteSpace($result.Output)) {
        throw "Unable to determine the candidate disposable instance's system_identifier -- refusing to proceed without it."
    }
    $result.Output.Trim()
}

function Assert-ApprovedDisposableInstance {
    # Fail-closed instance-identity verification: a URL, database name or
    # port is NEVER sufficient. Independently queries the two known
    # instances' real system_identifier (never a cached/hardcoded value)
    # and the candidate target's own, and requires the candidate to differ
    # from BOTH -- not merely "not named investment". Returns the identity
    # values so the caller can pass the SAME verified data on to the Java
    # test for it to independently re-check.
    param([Parameter(Mandatory = $true)]$ConnectionInfo)
    $mainId = Get-KnownPostgresSystemIdentifier -Alias "MAIN_DB"
    $etfTestId = Get-KnownPostgresSystemIdentifier -Alias "ETF_TEST_DB"
    $targetId = Get-MigrationTargetSystemIdentifier -ConnectionInfo $ConnectionInfo
    if ($targetId -eq $mainId) {
        throw "Refusing to run migration validation: the supplied -MigrationValidationDsn resolves to the SAME PostgreSQL instance as the main application database (system_identifier $targetId matches), even though its database name differs. A port-forward or alias to the same cluster is not a disposable instance."
    }
    if ($targetId -eq $etfTestId) {
        throw "Refusing to run migration validation: the supplied -MigrationValidationDsn resolves to the SAME PostgreSQL instance as the existing ETF test database (system_identifier $targetId matches), even though its database name differs."
    }
    [pscustomobject]@{
        TargetSystemIdentifier     = $targetId
        ForbiddenSystemIdentifiers = @($mainId, $etfTestId)
    }
}

function Invoke-ResearchMigrationMaven {
    # The single seam for delegating real migration/fixture/constraint
    # verification to the existing ResearchFlywayMigrationTest Java suite.
    # Mockable in Pester so no test in this repository ever actually shells
    # out to `mvn`.
    param([Parameter(Mandatory = $true)][string]$ProjectRoot, [Parameter(Mandatory = $true)][string[]]$Arguments)
    Push-Location $ProjectRoot
    try {
        $output = & mvn @Arguments 2>&1
        [pscustomobject]@{ ExitCode = $LASTEXITCODE; Output = ($output -join "`n") }
    } finally {
        Pop-Location
    }
}

function Test-MavenSurefireActuallyRan {
    # Guards against a Maven exit code of 0 that actually means "the test
    # was SKIPPED" (JUnit's own Assumptions.assumeTrue, or Surefire finding
    # no matching test at all) rather than "the test ran and passed". Only
    # an explicit "Tests run: 1, Failures: 0, Errors: 0, Skipped: 0" (or a
    # higher successful run count with zero skips) counts as real evidence.
    param([Parameter(Mandatory = $true)][string]$Output)
    $match = [regex]::Match($Output, "Tests run:\s*(\d+),\s*Failures:\s*(\d+),\s*Errors:\s*(\d+),\s*Skipped:\s*(\d+)")
    if (-not $match.Success) { return $false }
    $runCount = [int]$match.Groups[1].Value
    $failures = [int]$match.Groups[2].Value
    $errors = [int]$match.Groups[3].Value
    $skipped = [int]$match.Groups[4].Value
    return ($runCount -ge 1 -and $failures -eq 0 -and $errors -eq 0 -and $skipped -eq 0)
}

function Get-MigrationValidationPlan {
    # Pure/read-only: builds the plan object printed in both dry-run and
    # authorized mode, before anything mutating happens. Never touches a
    # database -- only the local migration files.
    param(
        [Parameter(Mandatory = $true)][string]$ProjectRoot,
        [string]$MigrationValidationDsn,
        [switch]$AuthorizeMigrationValidation
    )
    $files = Get-ResearchMigrationFiles -ProjectRoot $ProjectRoot
    $baseline = $files | Where-Object { $_.Version -le 23 }
    $target = $files | Where-Object { $_.Version -eq 24 }
    if (-not $target) {
        throw "V24 migration not found under $ResearchMigrationDir -- nothing to validate yet."
    }
    $connectionSummary = "(none supplied -- dry-run does not require one)"
    if ($MigrationValidationDsn) {
        $parsed = ConvertFrom-PostgresConnectionString -Dsn $MigrationValidationDsn
        $connectionSummary = "$($parsed.User)@$($parsed.Host):$($parsed.Port)/$($parsed.Database) (password redacted)"
    }
    [pscustomobject]@{
        Mode                 = if ($AuthorizeMigrationValidation) { "AUTHORIZED EXECUTION" } else { "DRY RUN (default)" }
        BaselineVersionCount = $baseline.Count
        BaselineTopVersion   = ($baseline | Select-Object -Last 1).Version
        TargetMigration      = $target.Name
        Connection           = $connectionSummary
        ReservedNames        = $ReservedResearchMigrationDatabaseNames
        ForeignPrefix        = $ForeignMigrationDatabasePrefix
        DisposablePrefix     = $DisposableMigrationDatabasePrefix
        PlannedAssertions    = @(
            "Candidate instance's system_identifier differs from both the main and ETF-test instances (independently queried)",
            "V1..V23 baseline applies cleanly via real Flyway (not a raw-SQL replay)",
            "Valid BASELINE/DEEP checkpoint fixtures are inserted, satisfying every NOT NULL/FK/CHECK column",
            "ETF_ONBOARD insertion is rejected before V24, specifically by SQLSTATE 23514 and the named phase CHECK constraint",
            "V24 applies via real Flyway: exactly one migration executed, recorded successfully in flyway_schema_history_research",
            "The BASELINE/DEEP fixture rows are byte-for-byte unchanged after V24",
            "ETF_ONBOARD is accepted after V24",
            "An unrecognized phase is still rejected by the same named constraint after V24",
            "Re-invoking Flyway executes zero new migrations (real idempotence, not a second raw SQL execution)"
        )
    }
}

function Write-MigrationValidationPlan {
    param([Parameter(Mandatory = $true)]$Plan)
    Write-Output "=== RESEARCH MIGRATION VALIDATION PLAN ==="
    Write-Output "Mode:                 $($Plan.Mode)"
    Write-Output "Baseline migrations:  V1..V$($Plan.BaselineTopVersion) ($($Plan.BaselineVersionCount) files)"
    Write-Output "Target migration:     $($Plan.TargetMigration)"
    Write-Output "Connection:           $($Plan.Connection)"
    Write-Output "Reserved databases (never touched): $($Plan.ReservedNames -join ', ')"
    Write-Output "Rejected foreign prefix (Python suite's own tests): $($Plan.ForeignPrefix)*"
    Write-Output "This run's disposable database will be named: $($Plan.DisposablePrefix)<10 hex chars>"
    Write-Output "Assertions this run will make (via the real Flyway Java API, see ResearchFlywayMigrationTest):"
    foreach ($assertion in $Plan.PlannedAssertions) { Write-Output "  - $assertion" }
}

function Invoke-PlatformValidateResearchMigration {
    # Entry point dispatched from platform.ps1's `validate-research-migration`
    # command. Dry-run (no -AuthorizeMigrationValidation) is the default and
    # the ONLY path that requires nothing but the local checkout. The
    # authorized path independently verifies instance identity, creates a
    # disposable database, and delegates all migration/fixture/constraint
    # verification to the real Flyway Java API via `mvn`. Cleanup is a
    # wholly separate action -- see -AuthorizeMigrationCleanup.
    param(
        [Parameter(Mandatory = $true)][string]$ProjectRoot,
        [string]$MigrationValidationDsn,
        [switch]$AuthorizeMigrationValidation,
        [switch]$AuthorizeMigrationCleanup,
        [string]$CleanupDatabaseName
    )

    if ($AuthorizeMigrationCleanup) {
        # A wholly separate action from validation: never reachable from the
        # validation path above, never implied by a successful or failed run.
        if (-not $MigrationValidationDsn) { throw "-AuthorizeMigrationCleanup requires -MigrationValidationDsn (the same disposable server the database was created on)." }
        if (-not $CleanupDatabaseName) { throw "-AuthorizeMigrationCleanup requires -CleanupDatabaseName (the exact disposable database name to remove)." }
        Test-SafeMigrationDatabaseName -Name $CleanupDatabaseName
        $connection = ConvertFrom-PostgresConnectionString -Dsn $MigrationValidationDsn
        Assert-SafeMigrationValidationTarget -DatabaseName $connection.Database
        $confirm = Invoke-ResearchMigrationPsql -ConnectionInfo $connection -Database "postgres" `
            -Sql "select 1 from pg_database where datname = '$CleanupDatabaseName';"
        if ($confirm.ExitCode -ne 0) { throw "Unable to verify '$CleanupDatabaseName' before cleanup: $($confirm.Output)" }
        if ($confirm.Output.Trim() -ne "1") { throw "Database '$CleanupDatabaseName' does not exist -- nothing to clean up, or it was already removed." }
        $drop = Invoke-ResearchMigrationPsql -ConnectionInfo $connection -Database "postgres" -Sql "drop database `"$CleanupDatabaseName`";"
        if ($drop.ExitCode -ne 0) { throw "DROP DATABASE failed for '$CleanupDatabaseName': $($drop.Output)" }
        Write-Output "Disposable migration-validation database '$CleanupDatabaseName' removed."
        return
    }

    $plan = Get-MigrationValidationPlan -ProjectRoot $ProjectRoot -MigrationValidationDsn $MigrationValidationDsn -AuthorizeMigrationValidation:$AuthorizeMigrationValidation
    Write-MigrationValidationPlan -Plan $plan

    if (-not $AuthorizeMigrationValidation) {
        Write-Output ""
        Write-Output "Dry run complete. No connection was opened and no SQL was executed."
        Write-Output "Re-run with -AuthorizeMigrationValidation -MigrationValidationDsn <disposable postgresql:// DSN> to execute this plan."
        return
    }

    if (-not $MigrationValidationDsn) {
        throw "-AuthorizeMigrationValidation requires -MigrationValidationDsn (a disposable PostgreSQL server; never the main or ETF test database)."
    }
    $connection = ConvertFrom-PostgresConnectionString -Dsn $MigrationValidationDsn
    Assert-SafeMigrationValidationTarget -DatabaseName $connection.Database

    # Fail-closed instance-identity verification BEFORE any database is
    # created. A URL, database name or port is never sufficient.
    $identity = Assert-ApprovedDisposableInstance -ConnectionInfo $connection

    $databaseName = New-DisposableMigrationDatabaseName
    Test-SafeMigrationDatabaseName -Name $databaseName
    Assert-SafeMigrationValidationTarget -DatabaseName $databaseName

    # Fail closed rather than reuse/overwrite anything that happens to
    # already exist under this (astronomically unlikely) generated name.
    $existsCheck = Invoke-ResearchMigrationPsql -ConnectionInfo $connection -Database "postgres" `
        -Sql "select 1 from pg_database where datname = '$databaseName';"
    if ($existsCheck.ExitCode -ne 0) {
        throw "Unable to verify database '$databaseName' does not already exist: $($existsCheck.Output)"
    }
    if ($existsCheck.Output.Trim() -eq "1") {
        throw "Generated disposable database name '$databaseName' already exists on the target server. Refusing to reuse or drop it; re-run to generate a new name."
    }

    $created = $false
    try {
        $createResult = Invoke-ResearchMigrationPsql -ConnectionInfo $connection -Database "postgres" -Sql "create database `"$databaseName`";"
        if ($createResult.ExitCode -ne 0) { throw "CREATE DATABASE failed: $($createResult.Output)" }
        $created = $true

        # All migration/fixture/constraint verification is delegated to the
        # real Flyway Java API via the existing JUnit test -- never
        # reimplemented here as raw SQL. Scoped to the research-service
        # module only (-pl, no -am) so a reactor module with no tests
        # (shared-domain/shared-web) never trips -DfailIfNoTests=true.
        $mavenArgs = @(
            "-pl", "services/research-service",
            "test",
            "-Dtest=ResearchFlywayEtfOnboardMigrationTest#etfOnboardMigrationUpgradesAndPreservesCheckpoints",
            "-DfailIfNoTests=true",
            "-DetfOnboardMigrationJdbcUrl=jdbc:postgresql://$($connection.Host):$($connection.Port)/$databaseName",
            "-DetfOnboardMigrationUsername=$($connection.User)",
            "-DetfOnboardMigrationPassword=$($connection.Password)",
            "-DetfOnboardMigrationApprovedSystemIdentifier=$($identity.TargetSystemIdentifier)",
            "-DetfOnboardMigrationForbiddenSystemIdentifiers=$($identity.ForbiddenSystemIdentifiers -join ',')"
        )
        $mavenResult = Invoke-ResearchMigrationMaven -ProjectRoot $ProjectRoot -Arguments $mavenArgs
        Write-Output $mavenResult.Output
        if ($mavenResult.ExitCode -ne 0) {
            throw "Real Flyway V23 -> V24 migration validation FAILED (mvn exit $($mavenResult.ExitCode)). Database '$databaseName' is preserved for inspection -- see cleanup instructions below."
        }
        if (-not (Test-MavenSurefireActuallyRan -Output $mavenResult.Output)) {
            throw "Maven reported success but Surefire shows the test was SKIPPED or never matched -- this proves nothing about real PostgreSQL migration compatibility. Database '$databaseName' is preserved for inspection -- see cleanup instructions below."
        }

        Write-Output ""
        Write-Output "All migration/fixture/constraint assertions passed via the real Flyway Java API against disposable database '$databaseName'."
    } finally {
        if ($created) {
            Write-Output ""
            Write-Output "Disposable database '$databaseName' was NOT dropped automatically -- it is preserved for evidence review."
            Write-Output "Once you've reviewed the results, remove it with a SEPARATE, separately-authorized command:"
            Write-Output "  .\platform.ps1 validate-research-migration -AuthorizeMigrationCleanup -CleanupDatabaseName $databaseName -MigrationValidationDsn <same disposable server DSN>"
        }
    }
}
