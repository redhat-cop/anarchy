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
app.kubernetes.io/instance: {{ .Release.Name }}
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
Create the operator domain to use
*/}}
{{- define "anarchy.operatorDomain" -}}
{{- if and .Values.operator_domain.generate .Values.operator_domain.name -}}
    {{ .Values.operator_domain.name }}
{{- else -}}
    {{ printf "%s.gpte.redhat.com" (default (include "anarchy.name" .) .Chart.Name) }}
{{- end -}}
{{- end -}}

{{/*
Define the operator image to deploy
*/}}
{{- define "anarchy.operator.image" -}}
{{- printf "%s:%s" .Values.image.anarchyOperator.repository (default .Chart.AppVersion .Values.image.anarchyOperator.tagOverride) -}}
{{- end -}}

{{/*
Define the runner image to deploy
*/}}
{{- define "anarchy.runner.image" -}}
{{- printf "%s:%s" .Values.image.anarchyRunner.repository (default .Chart.AppVersion .Values.image.anarchyRunner.tagOverride) -}}
{{- end -}}

{{/*
Define the anarchy FQDN
*/}}
{{- define "anarchy.hostname" -}}
{{- if .Values.openshift.enabled -}}
{{- default (printf "%s.%s" (include "anarchy.name" .) (regexReplaceAll "console." (lookup "route.openshift.io/v1" "Route" "openshift-console" "console").spec.host "${1}")) .Values.openshift.route.host -}}
{{- end -}}
{{- end -}}
