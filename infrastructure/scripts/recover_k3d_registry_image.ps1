# Recover an exact cached OCI image. Read-only unless -Publish is specified.
[CmdletBinding()]
param(
 [Parameter(Mandatory=$true)]
 [ValidateSet('api-gateway','auth-service','broker-service','company-service','frontend',
 'ibkr-connector','mcp-gateway','notification-service','portfolio-optimizer',
 'portfolio-service','ranking-engine','recommendation-service','research-service',
 'risk-service','valuation-engine','yahoo-finance-mcp')]
 [string]$Service,
 [switch]$Publish
)
$ErrorActionPreference='Stop'
$node='k3d-ai-investment-dev-server-0'
$socket='/run/k3s/containerd/containerd.sock'
$tag='dev-20261103-152626'
$source='localhost:5001/ai-investment/'+$Service+':'+$tag
$target='ai-investment-registry:5000/ai-investment/'+$Service+':'+$tag
$url='http://localhost:5001/v2/ai-investment/'+$Service+'/manifests/'+$tag
# Approved index digests recorded from containerd. Do not substitute rebuilt images.
$expected=@{
 'api-gateway'='63a8c916c63745d172dc8e6635c1b89ab80ffdc08f0225f0382d2ee0139cb2a3'
 'auth-service'='744c68d9975528ca81a5e31498568a48ac76f700eeab6a3b2dc6b52f287be5eb'
 'broker-service'='58721d58e0a07eb4b5f5c894df0058968b8563864b719362ae47d165f18222a2'
 'company-service'='d38cf3e55facabb389d4006b98894c60843ed4db72c9610d195bc7dddb2a6599'
 'frontend'='11a3b837ba8a08f38ad97513f7feba829ffe6c0e5ff0b5f432ee415336bc62d8'
 'ibkr-connector'='eeaecc04bb084ee4971c7279e15b6a48d0be44a2e7bb22d854ff18ebe285a181'
 'mcp-gateway'='f5f3aacfdefea6b300283bf575956221dd37021d140c516910ef2748b0bcf2ab'
 'notification-service'='30734833110ae822a3efe5b91e7e6ebe7614d15bf02c119f77cf38227f715cc5'
 'portfolio-optimizer'='6e4b86b567f9656db4ae735a07189573cd273d2579a4a4a683ba30fa21463e56'
 'portfolio-service'='1997118b5e610bc9f0b39637085a603738a39c179321e98409a6667f38b581f0'
 'ranking-engine'='08eae00c9e776a2bfdce962db604cf64db505c50c844f696f6f3499ae371f1d3'
 'recommendation-service'='9aa3faf56fa292b0666680be9e1857527d9004c766c93902077f7f14cb3adf3d'
 'research-service'='786a6cbe7900a18d068f46471db0640129f05175ff0a4091b87a4e45704c014a'
 'risk-service'='4d329165614781c9aa00cc678bd9ec719e8523aaa8bca6193a83f1d2304611b6'
 'valuation-engine'='14f8f39f9c5b2cd6e1b2c596247b0ad8f983cb74047e5b6d31b7360b011660ad'
 'yahoo-finance-mcp'='2ce8fdc461c28da185ae83fb3bb539fd8e1c534aa7729085c32b8b810d159db7'
}
function ImageRow([string]$ref) {
 $rows=& docker exec $node /bin/ctr --address $socket --namespace k8s.io images ls 2>&1
 if($LASTEXITCODE -ne 0){throw 'CONTAINERD_IMAGES_LIST_FAILED'}
 foreach($line in $rows){
  $parts=([string]$line).Trim() -split '\s+'
  if($parts.Count -ge 3 -and $parts[0] -ceq $ref){return ,$parts}
 }
 return $null
}
function RegistryDigest {
 $headers=& curl.exe --silent --show-error --include --head --header 'Accept: application/vnd.oci.image.index.v1+json' $url 2>&1
 if($LASTEXITCODE -ne 0){throw 'REGISTRY_HEAD_FAILED'}
 $statuses=@($headers | Where-Object {$_ -match '^HTTP/\S+\s+\d{3}'})
 if($statuses.Count -lt 1){throw 'REGISTRY_STATUS_MISSING'}
 $status=[int]([regex]::Match([string]$statuses[-1],'^HTTP/\S+\s+(\d{3})').Groups[1].Value)
 if($status -eq 404){return $null}
 if($status -ne 200){throw "REGISTRY_UNEXPECTED_HTTP_$status"}
 $lines=@($headers | Where-Object {$_ -match '^Docker-Content-Digest:\s*(sha256:[a-f0-9]{64})\s*$'})
 if($lines.Count -ne 1){throw 'REGISTRY_DIGEST_MISSING'}
 return [regex]::Match([string]$lines[0], 'sha256:[a-f0-9]{64}').Value
}
$want='sha256:'+$expected[$Service]
$row=ImageRow $source
if($null -eq $row -or $row[2] -cne $want){throw 'ORIGINAL_IMAGE_DIGEST_MISMATCH'}
$checks=& docker exec $node /bin/ctr --address $socket --namespace k8s.io images check 2>&1
if($LASTEXITCODE -ne 0){throw 'IMAGE_CHECK_FAILED'}
$complete=@($checks | Where-Object {
 ([string]$_).StartsWith("$source ") -and
 ([string]$_) -match '\bcomplete\s*\(\d+/\d+\)' -and
 ([string]$_) -match '\btrue\s*$'
})
if($complete.Count -ne 1){throw 'ORIGINAL_IMAGE_NOT_COMPLETE'}
$existing=RegistryDigest
if($null -ne $existing){
 if($existing -cne $want){throw 'EXISTING_REGISTRY_DIGEST_CONFLICT'}
 Write-Host "REGISTRY_IMAGE_ALREADY_VERIFIED: $Service"
 exit 0
}
Write-Host "CACHED_IMAGE_VERIFIED: $Service"
if(-not $Publish){Write-Host 'READ_ONLY_PREFLIGHT_PASSED'; exit 0}
$alias=ImageRow $target
if($null -ne $alias){
 if($alias[2] -cne $want){throw 'INTERNAL_ALIAS_DIGEST_CONFLICT'}
}else{
 & docker exec $node /bin/ctr --address $socket --namespace k8s.io images tag $source $target
 if($LASTEXITCODE -ne 0){throw 'IMAGE_ALIAS_FAILED'}
}
# --local is required for --http-trace and avoids the transfer-service push path.
# Never record HTTP trace: upload URLs can contain sensitive session state.
& docker exec $node /bin/ctr --address $socket --namespace k8s.io images push --local --plain-http $target
if($LASTEXITCODE -ne 0){throw 'IMAGE_PUSH_FAILED'}
$published=RegistryDigest
if($published -cne $want){throw 'RECOVERED_DIGEST_MISMATCH'}
Write-Host "REGISTRY_IMAGE_RECOVERED_AND_DIGEST_VERIFIED: $Service"
