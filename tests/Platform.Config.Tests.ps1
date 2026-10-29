# Regression tests for platform.ps1 Select-ClusterContext kubeconfig fix.
#
# Root cause: k3d v5.x `k3d kubeconfig merge <cluster> --kubeconfig-switch-context`
# exits 0 but does NOT write into the default ~/.kube/config unless
# --kubeconfig-merge-default is also passed. After a cluster restart that
# changes the load-balancer host port, platform.ps1 would fail at
# Ensure-Namespace with "connection refused" because the kubeconfig server URL
# was stale.
#
# These tests are read-only: they parse platform.ps1 source text only.
# They do NOT import or execute platform.ps1, do NOT start/stop clusters,
# and do NOT modify any kubeconfig files.

Describe 'platform.ps1 Select-ClusterContext kubeconfig merge' {
    BeforeAll {
        $scriptPath = Join-Path $PSScriptRoot '..\platform.ps1'
        $scriptPath = [System.IO.Path]::GetFullPath($scriptPath)
        $source = Get-Content -Raw -Path $scriptPath
    }

    It 'Contains the Select-ClusterContext function' {
        $source | Should -Match 'function\s+Select-ClusterContext'
    }

    It 'Uses --kubeconfig-merge-default to write the live API endpoint into the default kubeconfig' {
        $source | Should -Match 'k3d\s+kubeconfig\s+merge\s+\$ClusterName\s+--kubeconfig-merge-default\s+--kubeconfig-switch-context'
    }

    It 'Does NOT use the old broken command (merge without --kubeconfig-merge-default)' {
        # The old command was: k3d kubeconfig merge $ClusterName --kubeconfig-switch-context
        # (no --kubeconfig-merge-default). This pattern should NOT match.
        $oldPattern = 'k3d\s+kubeconfig\s+merge\s+\$ClusterName\s+--kubeconfig-switch-context'
        $source | Should -Not -Match $oldPattern
    }

    It 'Preserves --kubeconfig-switch-context for context switching' {
        $source | Should -Match 'k3d\s+kubeconfig\s+merge\s+\$ClusterName\s+--kubeconfig-merge-default\s+--kubeconfig-switch-context'
    }

    It 'Preserves cluster identity (ai-investment-dev)' {
        $source | Should -Match '\$ClusterName\s*=\s*"ai-investment-dev"'
    }

    It 'Preserves namespace identity (ai-investment)' {
        $source | Should -Match '\$Namespace\s*=\s*"ai-investment"'
    }
}
