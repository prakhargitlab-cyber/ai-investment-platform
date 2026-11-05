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
