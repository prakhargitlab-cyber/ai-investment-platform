{{- define "aip.name" -}}
{{- .Chart.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "aip.labels" -}}
app.kubernetes.io/name: {{ include "aip.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "aip.image" -}}
{{- $registry := .root.Values.global.imageRegistry -}}
{{- if $registry -}}{{ $registry }}/{{ .image }}:{{ .tag }}{{- else -}}{{ .image }}:{{ .tag }}{{- end -}}
{{- end -}}
