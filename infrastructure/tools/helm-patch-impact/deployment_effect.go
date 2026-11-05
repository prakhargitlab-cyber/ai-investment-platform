package main

import (
    "bytes"
    "encoding/json"

    appsv1 "k8s.io/api/apps/v1"
    "k8s.io/apimachinery/pkg/util/strategicpatch"
)

// VerifiedNoopDeploymentPatch determines whether the actual strategic patch,
// applied to the CURRENT Deployment, would leave the entire Deployment spec
// exactly unchanged. Metadata/unknown operations always block.
// This permits only semantically redundant spec patches; it cannot authorize
// any container image, env, replicas, or pod template change.
// It does not claim that Helm would issue zero API requests.
func verifiedNoopDeploymentPatch(t triple, patch []byte) bool {
    var root map[string]json.RawMessage
    if json.Unmarshal(patch, &root) != nil || len(root) != 1 {
        return false
    }
    rawSpec, ok := root["spec"]
    if !ok || bytes.Equal(bytes.TrimSpace(rawSpec), []byte("null")) {
        return false
    }
    var obj map[string]json.RawMessage
    if json.Unmarshal(t.Live, &obj) != nil { return false }
    liveSpec, present := obj["spec"]
    if !present { return false }
    patched, err := strategicpatch.StrategicMergePatch(
        t.Live, patch, &appsv1.Deployment{})
    if err != nil { return false }
    var after map[string]json.RawMessage
    if json.Unmarshal(patched, &after) != nil { return false }
    afterSpec, present := after["spec"]
    if !present { return false }
    // Compare parsed values: field ordering is irrelevant.
    var beforeValue, afterValue interface{}
    if json.Unmarshal(liveSpec, &beforeValue) != nil ||
       json.Unmarshal(afterSpec, &afterValue) != nil { return false }
    beforeJSON, err := json.Marshal(beforeValue)
    if err != nil { return false }
    afterJSON, err := json.Marshal(afterValue)
    if err != nil { return false }
    return bytes.Equal(beforeJSON, afterJSON)
}
