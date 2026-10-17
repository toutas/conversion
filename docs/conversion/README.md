# Conversion
The implemented demonstration converts a Jenkinsfile through two agents with different
contexts: a converter proposes a workflow, and a reviewer examines it. Ordinary
code controls the iterations and performs static checks.

The implementation is a very simplified demo. Example inputs are the [basic](../../tests/fixtures/basic.Jenkinsfile) and
[container/parallel](../../tests/fixtures/container-parallel.Jenkinsfile) examples.

## Diagrams


[Demo behavior](demo-behavior.md) - describes behavior implemented in `/src`