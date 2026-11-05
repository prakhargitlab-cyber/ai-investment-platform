package main

import (
    "encoding/json"
    "sort"
)

// researchPatchLabels examines the actual strategic merge PATCH, not a
// desired-vs-live approximation. Output uses fixed labels only.
func researchPatchLabels(raw []byte) ([]string, error) {
    var root interface{}
    if err := json.Unmarshal(raw, &root); err != nil { return nil, err }
    found := map[string]bool{}
    add := func(s string) { found[s] = true }
    allowedEnv := map[string]bool{
        "AIP_RESEARCH_EQUITY_MANUAL_EXECUTION_ENABLED":true,
        "AIP_RESEARCH_ETF_MANUAL_EXECUTION_ENABLED":true,
        "AIP_RESEARCH_ETF_FULL_UNIVERSE_EXECUTION_ENABLED":true,
        "AIP_RESEARCH_OPPORTUNITY_RECOVERY_PAUSED":true,
        "AIP_RESEARCH_OPPORTUNITY_SCHEDULER_ENABLED":true,
    }
    var walk func(interface{}, string)
    walk = func(v interface{}, context string) {
        switch value := v.(type) {
        case map[string]interface{}:
            for k, child := range value {
                if len(k) > 0 && k[0] == '$' {
                    add("STRATEGIC_LIST_DIRECTIVE")
                    continue
                }
                switch context {
                case "root":
                    if k == "spec" { walk(child,"deployment-spec") } else { add("OTHER_TOP_LEVEL") }
                case "deployment-spec":
                    if k == "template" { walk(child,"pod-template") } else { add("OTHER_DEPLOYMENT_SPEC") }
                case "pod-template":
                    if k == "spec" { walk(child,"pod-spec") } else { add("OTHER_POD_TEMPLATE") }
                case "pod-spec":
                    if k == "containers" || k == "initContainers" { walk(child,"container-list") } else { add("OTHER_POD_SPEC") }
                case "container":
                    switch k {
                    case "name":
                    case "image": add("CONTAINER_IMAGE")
                    case "env": walk(child,"env-list")
                    default: add("OTHER_CONTAINER_FIELD")
                    }
                case "env-item":
                    switch k {
                    case "name":
                        if s, ok := child.(string); !ok || !allowedEnv[s] {
                            add("OTHER_ENV_NAME")
                        } else { add("APPROVED_ENV_NAME") }
                    case "value":
                        // Recognizing a name is not proof that the value is approved.
                        add("ENV_VALUE_PATCH")
                    default: add("OTHER_ENV_FIELD")
                    }
                default:
                    add("OTHER_NESTED_FIELD")
                }
            }
        case []interface{}:
            for _, elem := range value {
                switch context {
                case "container-list":
                    walk(elem,"container")
                case "env-list":
                    walk(elem,"env-item")
                default:
                    add("OTHER_LIST_PATCH")
                }
            }
        default:
            add("NONOBJECT_PATCH")
        }
    }
    walk(root,"root")
    out := make([]string,0,len(found))
    for label := range found { out=append(out,label) }
    sort.Strings(out)
    return out,nil
}


// researchEnvAudit returns fixed labels about the PATCH's env item changes.
// A list-order directive is treated as a distinct signal, never as an env edit.
// No names or values from the patch are emitted in diagnostics.
func researchEnvAudit(patch []byte) ([]string, error) {
    var root map[string]interface{}
    if err := json.Unmarshal(patch, &root); err != nil { return nil, err }
    labels := map[string]bool{}
    allowed := map[string]bool{
        "AIP_RESEARCH_EQUITY_MANUAL_EXECUTION_ENABLED": true,
        "AIP_RESEARCH_ETF_MANUAL_EXECUTION_ENABLED": true,
        "AIP_RESEARCH_ETF_FULL_UNIVERSE_EXECUTION_ENABLED": true,
        "AIP_RESEARCH_OPPORTUNITY_RECOVERY_PAUSED": true,
        "AIP_RESEARCH_OPPORTUNITY_SCHEDULER_ENABLED": true,
    }
    spec, ok := root["spec"].(map[string]interface{})
    if !ok { return []string{}, nil }
    tmpl, ok := spec["template"].(map[string]interface{})
    if !ok { return []string{}, nil }
    pod, ok := tmpl["spec"].(map[string]interface{})
    if !ok { return []string{}, nil }
    for _, kind := range []string{"containers","initContainers"} {
        if _, present := pod["$setElementOrder/"+kind]; present {
            labels["CONTAINER_ORDER_DIRECTIVE"] = true
        }
        containers, ok := pod[kind].([]interface{})
        if !ok { continue }
        for _, candidate := range containers {
            item, ok := candidate.(map[string]interface{})
            if !ok { labels["MALFORMED_CONTAINER_PATCH"] = true; continue }
            if _, present := item["$setElementOrder/env"]; present {
                labels["ENV_ORDER_DIRECTIVE"] = true
            }
            envList, exists := item["env"]
            if !exists { continue }
            envs, ok := envList.([]interface{})
            if !ok { labels["MALFORMED_ENV_PATCH"] = true; continue }
            for _, env := range envs {
                field, ok := env.(map[string]interface{})
                if !ok { labels["MALFORMED_ENV_PATCH"] = true; continue }
                name, named := field["name"].(string)
                if !named { labels["MALFORMED_ENV_PATCH"] = true; continue }
                if len(field) == 1 {
                    labels["ENV_NAME_ONLY_PATCH_ENTRY"] = true
                } else if allowed[name] {
                    labels["APPROVED_ENV_PATCH_ENTRY"] = true
                } else {
                    labels["UNAPPROVED_ENV_PATCH_ENTRY"] = true
                }
            }
        }
    }
    out := make([]string,0,len(labels))
    for label := range labels { out=append(out,label) }
    sort.Strings(out)
    return out,nil
}
