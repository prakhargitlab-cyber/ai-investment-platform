# Script builder shared by production and the disposable PostgreSQL validation.
function New-SynchronizedSnapshotScript {
    param([string]$DatabaseUser, [string]$DatabaseName, [string]$RemoteDumpPath)
    Test-SafeIdentifier $DatabaseUser 'Database user'
    Test-SafeIdentifier $DatabaseName 'Database name'
    if ($RemoteDumpPath -notmatch '^/tmp/[A-Za-z0-9_.-]+[.]dump$') { throw 'Unsafe remote dump path' }
    $template = @'
set -eu
work=$(mktemp -d /tmp/platform-snapshot.XXXXXXXX)
trap 'rm -rf "$work"' EXIT HUP INT TERM
export PLATFORM_DUMP_STATUS="$work/dump-status"
psql -X -qAt -v ON_ERROR_STOP=1 -U __USER__ -d __DB__ <<'PLATFORM_SQL_EOF'
BEGIN ISOLATION LEVEL REPEATABLE READ;
SELECT pg_export_snapshot() AS snap
\gset
\setenv PLATFORM_SNAPSHOT :snap
\! pg_dump --snapshot="$PLATFORM_SNAPSHOT" -Fc -U __USER__ -d __DB__ -f __DUMP__ ; printf '%s' "$?" > "$PLATFORM_DUMP_STATUS"
SELECT format('SELECT %L || count(*) FROM %I.%I;', table_schema || '.' || table_name || '|', table_schema, table_name)
FROM information_schema.tables
WHERE table_schema NOT IN ('pg_catalog','information_schema') AND table_type = 'BASE TABLE'
ORDER BY table_schema, table_name
\gexec
COMMIT;
PLATFORM_SQL_EOF
test -s "$PLATFORM_DUMP_STATUS"
exit "$(cat "$PLATFORM_DUMP_STATUS")"
'@
    return $template.Replace('__USER__', $DatabaseUser).Replace('__DB__', $DatabaseName).Replace('__DUMP__', $RemoteDumpPath)
}
