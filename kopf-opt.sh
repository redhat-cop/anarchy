#!/bin/sh
KOPF_OPTIONS="--log-format=json"

# Default ANARCHY_SERVICE to HOSTNAME for running in odo
ANARCHY_SERVICE=${ANARCHY_SERVICE:-$HOSTNAME}

# Restrict watch to operator namespace.
KOPF_NAMESPACE=$(cat /run/secrets/kubernetes.io/serviceaccount/namespace)

# Match Kopf peering object to Anarchy Service name
KOPF_PEERING=${ANARCHY_SERVICE}
