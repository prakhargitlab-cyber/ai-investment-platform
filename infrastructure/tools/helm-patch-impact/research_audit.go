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
