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
# Recovery uses an OCI archive and direct registry uploads, never ctr images push.
# Keep source index + unknown/unknown attestation, preserving the original index digest.
function CheckBlob([string]$root,[string]$digest,[long]$size=-1) {
 if($digest -cnotmatch '^sha256:[a-f0-9]{64}$'){throw 'INVALID_DESCRIPTOR_DIGEST'}
 $file=Join-Path $root ('blobs/sha256/'+$digest.Substring(7))
 if(-not (Test-Path -LiteralPath $file -PathType Leaf)){throw 'ARCHIVE_CONTENT_MISSING'}
 if($size -ge 0 -and (Get-Item -LiteralPath $file).Length -ne $size){throw 'ARCHIVE_SIZE_MISMATCH'}
 $hash=(Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash.ToLowerInvariant()
 if(('sha256:'+$hash) -cne $digest){throw 'ARCHIVE_HASH_MISMATCH'}
 return $file
}
function WebHead([string]$uri,[string]$accept='') {
 $params=@{Uri=$uri;Method='Head';UseBasicParsing=$true;TimeoutSec=20}
 if($accept){$params.Headers=@{Accept=$accept}}
 try {return Invoke-WebRequest @params}
 catch {
  if($null -ne $_.Exception.Response -and [int]$_.Exception.Response.StatusCode -eq 404){return $null}
  throw
 }
}
function AssertResponseDigest($response,[string]$expectedDigest) {
 $got=[string]$response.Headers['Docker-Content-Digest']
 if($got -cne $expectedDigest){throw 'REGISTRY_PUBLISHED_DIGEST_MISMATCH'}
}
function PutManifest([string]$name,[string]$file,[string]$type,[string]$expectedDigest) {
 if($type -notin @('application/vnd.oci.image.manifest.v1+json','application/vnd.oci.image.index.v1+json')){throw 'UNSUPPORTED_MANIFEST_TYPE'}
 $res=Invoke-WebRequest -Uri ($repo+'/manifests/'+$name) -Method Put -InFile $file -ContentType $type -UseBasicParsing -TimeoutSec 120
 if($res.StatusCode -ne 201){throw 'REGISTRY_MANIFEST_PUT_FAILED'}
 AssertResponseDigest $res $expectedDigest
}
$stamp=[DateTime]::UtcNow.ToString('yyyyMMddHHmmssfff')
$folder=Join-Path (Get-Location).Path ('.tmp/registry-recovery/'+$Service+'-'+$stamp)
New-Item -ItemType Directory -Path $folder -ErrorAction Stop | Out-Null
$remote='/tmp/oci-recovery-'+$Service+'-'+$stamp+'.tar'
$archive=Join-Path $folder 'image.oci.tar'
$root=Join-Path $folder 'oci'
New-Item -ItemType Directory -Path $root | Out-Null
& docker exec $node /bin/ctr --address $socket --namespace k8s.io images export --local --all-platforms --skip-manifest-json $remote $source
if($LASTEXITCODE -ne 0){throw 'CONTAINERD_OCI_EXPORT_FAILED'}
& docker cp ($node+':'+$remote) $archive
if($LASTEXITCODE -ne 0){throw 'OCI_ARCHIVE_COPY_FAILED'}
if(-not (Get-Command tar.exe -ErrorAction SilentlyContinue)){throw 'WINDOWS_TAR_NOT_FOUND'}
& tar.exe -xf $archive -C $root
if($LASTEXITCODE -ne 0){throw 'OCI_ARCHIVE_EXTRACT_FAILED'}
$layout=Get-Content -LiteralPath (Join-Path $root 'index.json') -Raw | ConvertFrom-Json
$refs=@($layout.manifests)
if($refs.Count -ne 1 -or [string]$refs[0].digest -cne $want -or [string]$refs[0].mediaType -cne 'application/vnd.oci.image.index.v1+json'){throw 'ARCHIVE_ROOT_INDEX_MISMATCH'}
$indexFile=CheckBlob $root $want ([long]$refs[0].size)
$index=Get-Content -LiteralPath $indexFile -Raw | ConvertFrom-Json
if([string]$index.mediaType -cne 'application/vnd.oci.image.index.v1+json'){throw 'OCI_INDEX_TYPE_MISMATCH'}
$children=@($index.manifests)
if($children.Count -lt 1){throw 'OCI_INDEX_CHILDREN_MISSING'}
$manifests=@()
$blobs=@{}
foreach($descriptor in $children){
 if([string]$descriptor.mediaType -cne 'application/vnd.oci.image.manifest.v1+json'){throw 'UNEXPECTED_INDEX_CHILD_MEDIA_TYPE'}
 $mf=CheckBlob $root ([string]$descriptor.digest) ([long]$descriptor.size)
 $doc=Get-Content -LiteralPath $mf -Raw | ConvertFrom-Json
 if([string]$doc.mediaType -cne 'application/vnd.oci.image.manifest.v1+json'){throw 'CHILD_MANIFEST_MEDIA_TYPE_MISMATCH'}
 $null=CheckBlob $root ([string]$doc.config.digest) ([long]$doc.config.size)
 $blobs[[string]$doc.config.digest]=$true
 foreach($layer in $doc.layers){
  $null=CheckBlob $root ([string]$layer.digest) ([long]$layer.size)
  $blobs[[string]$layer.digest]=$true
 }
 $manifests+=@{Digest=[string]$descriptor.digest;File=$mf;Type=[string]$descriptor.mediaType}
}
$all=@(Get-ChildItem -LiteralPath (Join-Path $root 'blobs/sha256') -File)
foreach($f in $all){
 if($f.Name -cnotmatch '^[a-f0-9]{64}$'){throw 'UNEXPECTED_ARCHIVE_BLOB_NAME'}
 $null=CheckBlob $root ('sha256:'+$f.Name)
}
Write-Host ('OCI_ARCHIVE_ALL_BLOBS_VERIFIED: '+$all.Count)

# Upload only content blobs first. The registry accepts small POST -> PUT uploads.
$repo='http://localhost:5001/v2/ai-investment/'+$Service
foreach($digest in @($blobs.Keys | Sort-Object)){
 $exists=WebHead ($repo+'/blobs/'+$digest)
 if($null -ne $exists){continue}
 $start=Invoke-WebRequest -Method Post -Uri ($repo+'/blobs/uploads/') -UseBasicParsing -TimeoutSec 30
 if($start.StatusCode -ne 202){throw 'UPLOAD_SESSION_REJECTED'}
 $location=[string]$start.Headers['Location']
 if(-not $location){throw 'UPLOAD_LOCATION_MISSING'}
 $upload=[Uri]::new([Uri]($repo+'/blobs/uploads/'),$location)
 if($upload.Scheme -cne 'http' -or $upload.Host -cne 'localhost' -or $upload.Port -ne 5001){throw 'UNEXPECTED_UPLOAD_HOST'}
 $separator=if($upload.Query){'&'}else{'?'}
 $f=Join-Path $root ('blobs/sha256/'+$digest.Substring(7))
 $done=Invoke-WebRequest -Method Put -Uri ($upload.AbsoluteUri+$separator+'digest='+$digest) -InFile $f -ContentType 'application/octet-stream' -UseBasicParsing -TimeoutSec 180
 if($done.StatusCode -ne 201){throw 'REGISTRY_BLOB_UPLOAD_FAILED'}
 AssertResponseDigest $done $digest
 Write-Host ('BLOB_VERIFIED: '+$digest)
}
foreach($entry in $manifests){PutManifest $entry.Digest $entry.File $entry.Type $entry.Digest}
$existingTag=WebHead ($repo+'/manifests/'+$tag) 'application/vnd.oci.image.index.v1+json'
if($null -ne $existingTag){AssertResponseDigest $existingTag $want}
else{PutManifest $tag $indexFile 'application/vnd.oci.image.index.v1+json' $want}
$final=WebHead ($repo+'/manifests/'+$tag) 'application/vnd.oci.image.index.v1+json'
if($null -eq $final){throw 'FINAL_REGISTRY_INDEX_NOT_FOUND'}
AssertResponseDigest $final $want
Write-Host "REGISTRY_IMAGE_RECOVERED_AND_DIGEST_VERIFIED: $Service"
Write-Host ('AUDIT_ARCHIVE_DIRECTORY: '+$folder)
