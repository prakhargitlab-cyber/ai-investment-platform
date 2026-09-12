{{- define "aip.name" -}}
{{- .Chart.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "aip.labels" -}}
app.kubernetes.io/name: {{ include "aip.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "aip.selectorLabels" -}}
app.kubernetes.io/name: {{ include "aip.name" .root }}
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{- define "aip.componentLabels" -}}
{{ include "aip.labels" .root }}
app.kubernetes.io/component: {{ .component }}
app.kubernetes.io/part-of: ai-investment-platform
{{- end -}}

{{- define "aip.serviceAccountName" -}}
{{- if .Values.global.serviceAccount.name -}}
{{ .Values.global.serviceAccount.name }}
{{- else -}}
{{ include "aip.name" . }}
{{- end -}}
{{- end -}}

{{- define "aip.image" -}}
{{- $registry := .root.Values.global.imageRegistry -}}
{{- $tag := default .tag .root.Values.global.imageTag -}}
{{- if $registry -}}{{ $registry }}/{{ .image }}:{{ $tag }}{{- else -}}{{ .image }}:{{ $tag }}{{- end -}}
{{- end -}}

{{- define "aip.podLabels" -}}
{{ include "aip.selectorLabels" (dict "root" .root "component" .component) }}
{{- with .root.Values.global.podLabels }}
{{ toYaml . }}
{{- end }}
{{- if and .root.Values.global.azure.workloadIdentity.enabled (has .component .root.Values.global.azure.workloadIdentity.workloads) }}
azure.workload.identity/use: "true"
{{- end }}
{{- end -}}
{{- define "aip.podAnnotations" -}}
{{- with .root.Values.global.podAnnotations }}
{{ toYaml . }}
{{- end }}
{{- if and .root.Values.global.observability.otel.enabled (eq .language "java") .root.Values.global.observability.otel.javaAgentInjection }}
instrumentation.opentelemetry.io/inject-java: "true"
{{- end }}
{{- if and .root.Values.global.observability.otel.enabled (eq .language "python") .root.Values.global.observability.otel.pythonAgentInjection }}
instrumentation.opentelemetry.io/inject-python: "true"
{{- end }}
{{- end -}}

{{- define "aip.commonEnv" -}}
- name: AIP_ENVIRONMENT
  value: {{ .root.Values.global.environment | quote }}
- name: TZ
  value: {{ .root.Values.global.timezone | quote }}
- name: OTEL_SERVICE_NAME
  value: {{ .serviceName | quote }}
- name: OTEL_SDK_DISABLED
  value: {{ not .root.Values.global.observability.otel.enabled | quote }}
- name: OTEL_TRACES_EXPORTER
  value: {{ ternary "otlp" "none" (and .root.Values.global.observability.otel.enabled .root.Values.global.observability.otel.tracesEnabled) | quote }}
- name: OTEL_METRICS_EXPORTER
  value: {{ ternary "otlp" "none" (and .root.Values.global.observability.otel.enabled .root.Values.global.observability.otel.metricsEnabled) | quote }}
- name: OTEL_EXPORTER_OTLP_PROTOCOL
  value: {{ .root.Values.global.observability.otel.exporterOtlpProtocol | quote }}
- name: OTEL_EXPORTER_OTLP_ENDPOINT
  value: {{ .root.Values.global.observability.otel.exporterOtlpEndpoint | quote }}
- name: OTEL_RESOURCE_ATTRIBUTES
  value: {{ printf "deployment.environment.name=%s,%s" .root.Values.global.environment .root.Values.global.observability.otel.resourceAttributes | trimSuffix "," | quote }}
- name: AIP_FEATURE_MCP_ENABLED
  value: {{ .root.Values.featureFlags.mcpEnabled | quote }}
- name: AIP_FEATURE_LLM_ENABLED
  value: {{ .root.Values.featureFlags.llmEnabled | quote }}
- name: AIP_FEATURE_SCHEDULED_JOBS_ENABLED
  value: {{ .root.Values.featureFlags.scheduledJobsEnabled | quote }}
- name: AIP_FEATURE_GLOBAL_RESEARCH_ACQUISITION_ENABLED
  value: {{ .root.Values.featureFlags.globalResearchAcquisitionEnabled | quote }}
- name: AIP_OBJECT_STORAGE_ENABLED
  value: {{ .root.Values.objectStorage.enabled | quote }}
- name: AIP_OBJECT_STORAGE_PROVIDER
  value: {{ .root.Values.objectStorage.provider | quote }}
- name: AIP_OBJECT_STORAGE_ACCOUNT_NAME
  value: {{ .root.Values.objectStorage.accountName | quote }}
- name: AIP_OBJECT_STORAGE_ENDPOINT
  value: {{ .root.Values.objectStorage.endpoint | quote }}
- name: AIP_OBJECT_STORAGE_CONTAINER
  value: {{ .root.Values.objectStorage.container | quote }}
- name: AIP_OBJECT_STORAGE_AUTH_MODE
  value: {{ .root.Values.objectStorage.authMode | quote }}
- name: AIP_TEMPORARY_OBJECT_TTL_SECONDS
  value: {{ .root.Values.objectStorage.temporary.ttlSeconds | quote }}
{{- end -}}

{{- define "aip.keyVaultVolumeMount" -}}
{{- if and .root.Values.keyVault.enabled (has .component .root.Values.keyVault.workloads) }}
- name: key-vault-secrets
  mountPath: {{ .root.Values.keyVault.mountPath | quote }}
  readOnly: true
{{- end }}
{{- end -}}

{{- define "aip.keyVaultVolume" -}}
{{- if and .root.Values.keyVault.enabled (has .component .root.Values.keyVault.workloads) }}
- name: key-vault-secrets
  csi:
    driver: secrets-store.csi.k8s.io
    readOnly: true
    volumeAttributes:
      secretProviderClass: {{ .root.Values.keyVault.secretProviderClassName | quote }}
{{- end }}
{{- end -}}
