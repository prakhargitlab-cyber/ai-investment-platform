package main

import (
    "bytes"
    "encoding/base64"
    "encoding/json"
    "fmt"
)

// secretView decodes only Secret credential material. It is never logged.
type secretView struct {
    Data map[string]string `json:"data"`
    StringData map[string]string `json:"stringData"`
    Type string `json:"type"`
}

func persistedSecretData(raw json.RawMessage) (map[string][]byte, string, error) {
    var doc secretView
    if err := json.Unmarshal(raw, &doc); err != nil { return nil, "", err }
    values := make(map[string][]byte)
    for key, encoded := range doc.Data {
        decoded, err := base64.StdEncoding.Strict().DecodeString(encoded)
        if err != nil { return nil, "", fmt.Errorf("INVALID_SECRET_BASE64") }
        if base64.StdEncoding.EncodeToString(decoded) != encoded {
            return nil, "", fmt.Errorf("NONCANONICAL_SECRET_BASE64")
        }
        values[key] = decoded
    }
    for key, plain := range doc.StringData {
        if _, already := values[key]; already {
            return nil, "", fmt.Errorf("AMBIGUOUS_SECRET_DATA_KEY")
        }
        values[key] = []byte(plain)
    }
    if doc.Type == "" { doc.Type = "Opaque" }
    return values, doc.Type, nil
}

func equalSecretData(a, b map[string][]byte) bool {
    if len(a) != len(b) { return false }
    for key, value := range a {
        other, ok := b[key]
        if !ok || !bytes.Equal(value, other) { return false }
    }
    return true
}

// isVerifiedStringDataRemoval refuses every shape except {"stringData":null},
// AND independently compares all effective bytes and Secret types across
// Helm v1, candidate v2 and persisted live Secret.
// Removing write-only stringData from a Kubernetes Secret has no stored
// credential effect; any different patch is still a hard block.
func isVerifiedStringDataRemoval(t triple, patch []byte) bool {
    var obj map[string]json.RawMessage
    if json.Unmarshal(patch, &obj) != nil || len(obj) != 1 { return false }
    raw, exists := obj["stringData"]
    if !exists || !bytes.Equal(bytes.TrimSpace(raw), []byte("null")) {
        return false
    }
    oldData, oldType, err := persistedSecretData(t.Old)
    if err != nil { return false }
    newData, newType, err := persistedSecretData(t.New)
    if err != nil { return false }
    liveData, liveType, err := persistedSecretData(t.Live)
    if err != nil { return false }
    if oldType != newType || newType != liveType { return false }
    return equalSecretData(oldData, newData) && equalSecretData(newData, liveData)
}

 
// secretNoopFailure returns only a fixed diagnostic code. Never return
// credential bytes, map keys, decoded values, errors or raw patch material.
func secretNoopFailure(t triple, patch []byte) string {
    var obj map[string]json.RawMessage
    if json.Unmarshal(patch, &obj) != nil || len(obj) != 1 {
        return "PATCH_SHAPE"
    }
    field, present := obj["stringData"]
    if !present || !bytes.Equal(bytes.TrimSpace(field), []byte("null")) {
        return "PATCH_SHAPE"
    }
    oldData, oldType, err := persistedSecretData(t.Old)
    if err != nil { return "OLD_DATA_INVALID" }
    newData, newType, err := persistedSecretData(t.New)
    if err != nil { return "NEW_DATA_INVALID" }
    liveData, liveType, err := persistedSecretData(t.Live)
    if err != nil { return "LIVE_DATA_INVALID" }
    if oldType != newType || newType != liveType { return "TYPE_MISMATCH" }
    if !equalSecretData(oldData, newData) { return "OLD_TO_NEW_BYTES_MISMATCH" }
    if !equalSecretData(newData, liveData) { return "NEW_TO_LIVE_BYTES_MISMATCH" }
    return "VERIFIED"
}

 
// secretPatchShape classifies ONLY the top-level patch operation, never data.
// This is diagnostic information; it never creates a safety exemption.
func secretPatchShape(patch []byte) string {
    var fields map[string]json.RawMessage
    if json.Unmarshal(patch, &fields) != nil { return "INVALID_PATCH_JSON" }
    if len(fields) == 0 { return "EMPTY_PATCH" }
    raw, hasStringData := fields["stringData"]
    if !hasStringData { return "NO_STRINGDATA_FIELD" }
    if len(fields) != 1 { return "STRINGDATA_WITH_OTHER_OPERATIONS" }
    trimmed := bytes.TrimSpace(raw)
    if bytes.Equal(trimmed, []byte("null")) { return "STRINGDATA_NULL_ONLY" }
    if len(trimmed) != 0 && trimmed[0] == '{' {
        var entries map[string]json.RawMessage
        if json.Unmarshal(trimmed, &entries) != nil { return "STRINGDATA_MALFORMED_OBJECT" }
        if len(entries) == 0 { return "STRINGDATA_EMPTY_OBJECT" }
        allNull := true
        for _, v := range entries {
            if !bytes.Equal(bytes.TrimSpace(v), []byte("null")) { allNull = false }
        }
        if allNull { return "STRINGDATA_KEY_DELETIONS" }
        return "STRINGDATA_OBJECT_VALUES"
    }
    return "STRINGDATA_OTHER_SHAPE"
}


// isVerifiedLiveJWTDataRewrite recognizes a narrowly constrained v1
// stringData -> v2 data conversion that writes only the SAME bytes already
// persisted in the live JWT Secret. The old Helm stringData may be stale;
// it is never used to authorize a value change. This is JWT-only and
// must never exempt metadata, type, deletion or an unknown field operation.
func isVerifiedLiveJWTDataRewrite(t triple, patch []byte) bool {
    var oldRoot, newRoot map[string]json.RawMessage
    if json.Unmarshal(t.Old, &oldRoot) != nil ||
        json.Unmarshal(t.New, &newRoot) != nil { return false }
    if _, exists := oldRoot["stringData"]; !exists { return false }
    if _, exists := newRoot["stringData"]; exists { return false }
    if _, exists := newRoot["data"]; !exists { return false }

    var operations map[string]json.RawMessage
    if json.Unmarshal(patch, &operations) != nil ||
        (len(operations) != 1 && len(operations) != 2) {
        return false
    }
    if !bytes.Equal(bytes.TrimSpace(operations["stringData"]), []byte("null")) {
        return false
    }
    var patchData map[string]json.RawMessage
    if len(operations) == 2 {
        rawPatchData, exists := operations["data"]
        if !exists || json.Unmarshal(rawPatchData, &patchData) != nil ||
            len(patchData) == 0 {
            return false
        }
    }
    oldData, oldType, err := persistedSecretData(t.Old)
    if err != nil || len(oldData) == 0 { return false }
    nextData, nextType, err := persistedSecretData(t.New)
    if err != nil { return false }
    liveData, liveType, err := persistedSecretData(t.Live)
    if err != nil || oldType != nextType || nextType != liveType {
        return false
    }
    // A chart lookup must render exactly the currently persisted Secret.
    if !equalSecretData(nextData, liveData) { return false }
    if len(oldData) != len(nextData) { return false }
    for key := range oldData {
        if _, ok := nextData[key]; !ok { return false }
    }
    // Require every patch data entry to be an existing live key, with
    // canonical base64 and the exact unchanged bytes. No null/deletions.
    for key, encodedJSON := range patchData {
        var encoded string
        if json.Unmarshal(encodedJSON, &encoded) != nil { return false }
        decoded, err := base64.StdEncoding.Strict().DecodeString(encoded)
        if err != nil || base64.StdEncoding.EncodeToString(decoded) != encoded {
            return false
        }
        liveValue, exists := liveData[key]
        if !exists || !bytes.Equal(decoded, liveValue) { return false }
    }
    return true
}
