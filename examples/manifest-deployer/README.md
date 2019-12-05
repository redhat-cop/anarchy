= CakePHP Example

This example shows using Anarchy to deploy a sample application.

The example application shown is CakePHP (https://github.com/sclorg/cakephp-ex).
This repository includes an OpenShift template used to deploy the application.

== Usage

. Define `manifest-deployer` AnarchyGovernor
+
This AnarchyGovernor includes the Ansible tasks and default vars.
+
----
kubectl apply -f manifest-deployer.anarchygovernor.yaml
----

. Create `cakephp-ex` Namespace
+
The CakePHP example will be deployed in the `cakephp-ex` namespace.
+
----
kubectl create namespace cakephp-ex
----

. Define `cakephp-ex` Secret
+
This secret holds parameters that will be passed to the template to produce resource definitions.
+
----
kubectl apply -f cakephp-ex.secret.yaml
----

. Define `cakephp-ex` RoleBinding
+
This RoleBinding grants edit access for service accounts in the `anarchy-operator` namespace.
The `anarchy-operator` service account needs access to read secrets while the `anarchy-default-runner` needs access to create the templated resources.
+
----
kubectl apply -f cakephp-ex.rolebinding.yaml
----

. Define `cakephp-ex` AnarchySubject
+
Creating the subject triggers the Anarchy operator to process the template using the Ansible tasks in the governor.
+
----
kubectl apply -f cakephp-ex.anarchysubject.yaml
----

A `playbook.yaml` is provided to run these tasks.

----
ansible-playbook playbook.yaml
----
