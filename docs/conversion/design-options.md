# Design options for the conversion module

## Richer deterministic workflow

```mermaid
flowchart TD
    Input["Jenkinsfile, relevant context and permissions"]

    subgraph Workflow["Code-controlled conversion workflow"]
        Harness["Harness: deterministic state transitions<br/>Schedule work, check evidence, retry or stop"]

        subgraph Conversion["Conversion task"]
            Converter["Converter agent: convert and revise"]
            ConverterTools["Converter tools and skills:<br/>deterministic Jenkins-to-Actions scripts,<br/>migration helpers and guidance"]
        end

        subgraph Review["Review task"]
            Reviewer["Reviewer agent: assess source, candidate and evidence"]
            ReviewerTools["Reviewer tools and skills:<br/>linters, comparison scripts,<br/>permitted tests and review guidance"]
        end

        Harness -->|Schedule conversion or revision| Converter
        Converter -->|Candidate, assumptions and gaps| Harness
        Converter <-->|Use permitted capabilities| ConverterTools
        Harness -->|Schedule review| Reviewer
        Reviewer -->|Findings, evidence and uncertainty| Harness
        Reviewer <-->|Use permitted capabilities| ReviewerTools
    end

    Input --> Harness
    Harness -->|Completion criteria met| Completed["Completed conversion:<br/>GitHub Actions workflow and supporting evidence"]
    Harness -->|Cannot reliably complete| Handoff["Human handoff:<br/>candidate if available, evidence,<br/>unresolved issues and next actions"]
```

The aim is to advance the Jenkins-to-GitHub-Actions conversion as far as the
available tools, context and permissions reliably support. A converter can
use a deterministic script to do supported translation work, not just ask an
LLM to write everything. A reviewer can use different capabilities to inspect
the candidate and gather evidence.

The harness declares completion only when its configured criteria are met by
the current candidate and evidence, not merely because an agent claims
success. If reliable completion is not supported, the handoff preserves any
useful candidate and explains what was achieved, what remains uncertain or
unsupported, and what a human needs to decide, supply or change. Completion
does not by itself mean deployment readiness or proven runtime equivalence.

## Tools-first vs Agents-first

```mermaid
flowchart LR
    subgraph ToolFirst["Alternative A: tool-first"]
        SourceA["Jenkinsfile"] --> ToolA["Suitable deterministic converter"]
        ToolA --> CoverageA["Candidate plus coverage and unsupported items"]
        CoverageA --> AgentsA["Agents handle gaps and review"]
        AgentsA --> EvidenceA["Recheck and report evidence"]
    end

    subgraph AgentLed["Alternative B: agent-first"]
        SourceB["Jenkinsfile"] --> AgentsB["Agent proposes conversion"]
        AgentsB --> ToolsB["Deterministic conversion helpers and checks"]
        ToolsB --> ReviewB["Review and revision"]
        ReviewB --> EvidenceB["Recheck and report evidence"]
    end
```