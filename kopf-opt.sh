#!/bin/sh

# Default ANARCHY_SERVICE to HOSTNAME for running in odo
ANARCHY_SERVICE=${ANARCHY_SERVICE:-$HOSTNAME}

# Restrict watch to operator namespace.
KOPF_NAMESPACED=true

# Do not attempt to coordinate with other kopf operators.
KOPF_STANDALONE=false
KOPF_OPTIONS="--peering=$ANARCHY_SERVICE"

KOPF_NAMESPACE="$(cat /run/secrets/kubernetes.io/serviceaccount/namespace)"
oc get kopfpeering anarchy || oc create -f - <<EOF
apiVersion: zalando.org/v1
kind: KopfPeering
metadata:
  namespace: $KOPF_NAMESPACE
  name: $ANARCHY_SERVICE
EOF
