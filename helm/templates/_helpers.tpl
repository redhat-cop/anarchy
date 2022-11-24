{{/* vim: set filetype=mustache: */}}
{{/*
Expand the name of the chart.
*/}}
{{- define "anarchy.name" -}}
{{-   default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Create chart name and version as used by the chart label.
*/}}
{{- define "anarchy.chart" -}}
{{-   printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Common labels
*/}}
{{- define "anarchy.labels" -}}
helm.sh/chart: {{ include "anarchy.chart" . }}
{{ include "anarchy.selectorLabels" . }}
{{-   if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{-   end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{/*
Selector labels
*/}}
{{- define "anarchy.selectorLabels" -}}
app.kubernetes.io/name: {{ include "anarchy.name" . }}
{{-   if (ne .Release.Name "RELEASE-NAME") }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{-   end -}}
{{- end -}}

{{/*
API component labels
*/}}
{{- define "anarchy.apiComponentLabels" -}}
{{-   include "anarchy.selectorLabels" . }}
app.kubernetes.io/component: api
{{- end -}}

{{/*
Operator component labels
*/}}
{{- define "anarchy.operatorComponentLabels" -}}
{{-   include "anarchy.selectorLabels" . }}
app.kubernetes.io/component: operator
{{- end -}}

{{/*
Create the name of the service account to use
*/}}
{{- define "anarchy.serviceAccountName" -}}
{{-   if .Values.serviceAccount.create -}}
{{      default (include "anarchy.name" .) .Values.serviceAccount.name }}
{{-   else -}}
{{      default "default" .Values.serviceAccount.name }}
{{-   end -}}
{{- end -}}

{{/*
Create the name of the namespace to use
*/}}
{{- define "anarchy.namespaceName" -}}
{{- if .Values.namespace.create -}}
    {{ default (include "anarchy.name" .) .Values.namespace.name }}
{{- else -}}
    {{ default "default" .Values.namespace.name }}
{{- end -}}
{{- end -}}

{{/*
Name to use for API
*/}}
{{- define "anarchy.apiName" -}}
{{-   printf "%s-api" (include "anarchy.name" .) -}}
{{- end -}}

