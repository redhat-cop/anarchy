#!/bin/bash

VERSION=$1

if [[  -z "${VERSION}" ]]; then
    echo "Usage: $0 VERSION"
    exit 1
fi

if [[ ! $VERSION =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "VERSION must be a semantic version: vMAJOR.MINOR.PATCH"
    exit 1
fi

if [[ 'master' != "$(git branch --show-current)" ]]; then
    echo "Not on master git branch!"
    exit 1
fi

if [[ -n "$(git tag -l $VERSION)" ]]; then
    echo "VERSION $VERSION already exists!"
    exit 1
fi

if [[ $VERSION != `(echo $VERSION; git tag | grep ^v[0-9]) | sort -V | tail -1` ]]; then
    echo "$VERSION is not semantically newest!"
    exit 1
fi

if [[ -n "$(git status --porcelain | grep -v '^?? ')" ]]; then
    echo "Cannot set version when working directory has differences"
fi

sed -i "s/^version: .*/version: ${VERSION:1}/" helm/Chart.yaml institutions/*/helm/Chart.yaml
sed -i "s/^appVersion: .*/appVersion: ${VERSION:1}/" helm/Chart.yaml institutions/*/helm/Chart.yaml

git add helm/Chart.yaml institutions/*/helm/Chart.yaml
git commit -m "Release $VERSION"
git tag $VERSION
git push origin master $VERSION
