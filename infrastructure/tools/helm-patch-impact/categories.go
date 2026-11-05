package main

import (
    "encoding/json"
    "sort"
)

// classifyPatch reports only fixed labels. Never print JSON patch values
// or arbitrary field keys (which could contain sensitive information).
func classifyPatch(kind string, raw []byte) ([]string, error) {
    var root map[string]interface{}
    if err := json.Unmarshal(raw, &root); err != nil { return nil, err }
    found := map[string]bool{}
    add := func(category string) { found[category] = true }
    if _, ok := root["metadata"]; ok { add("metadata") }
    if _, ok := root["type"]; ok { add("type") }
    if _, ok := root["data"]; ok { add("secret-data-or-config-data") }
    if _, ok := root["stringData"]; ok { add("secret-stringData") }
    if _, ok := root["binaryData"]; ok { add("binary-data") }
    for key := range root {
        switch key {
        case "metadata", "type", "data", "stringData", "binaryData", "spec":
        default:
            add("other")
        }
    }
    if spec, ok := root["spec"]; ok {
        specMap, isMap := spec.(map[string]interface{})
        if !isMap { add("spec-other") } else if kind != "Deployment" {
            add("spec")
        } else {
            for k := range specMap {
                if k != "template" { add("deployment-spec-other") }
            }
            template, present := specMap["template"]
            if present {
                tm, ok := template.(map[string]interface{})
                if !ok { add("pod-template-other") } else {
                    for k := range tm {
                        if k != "spec" && k != "metadata" { add("pod-template-other") }
                        if k == "metadata" { add("pod-template-metadata") }
                    }
                    pod, present := tm["spec"]
                    if present {
                        pm, ok := pod.(map[string]interface{})
                        if !ok { add("pod-spec-other") } else {
                            for k, v := range pm {
                                if k != "containers" && k != "initContainers" {
                                    add("pod-spec-other")
                                    continue
                                }
                                entries, ok := v.([]interface{})
                                if !ok { add("pod-spec-other"); continue }
                                for _, entry := range entries {
                                    container, ok := entry.(map[string]interface{})
                                    if !ok { add("container-other"); continue }
                                    for field := range container {
                                        switch field {
                                        case "name":
                                        case "image":
                                            add("container-image")
                                        case "env", "envFrom":
                                            add("container-env")
                                        default:
                                            add("container-other")
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
    }
    categories := make([]string, 0, len(found))
    for category := range found { categories = append(categories, category) }
    sort.Strings(categories)
    return categories, nil
}
