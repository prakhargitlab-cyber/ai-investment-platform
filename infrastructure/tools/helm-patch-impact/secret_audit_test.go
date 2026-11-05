package main

import (
    "encoding/json"
    "testing"
)

func secretTriple(oldPlain, proposedB64, liveB64 string) triple {
    old, _ := json.Marshal(map[string]interface{}{
        "apiVersion":"v1", "kind":"Secret",
        "metadata":map[string]string{"name":"dev-postgres"},
        "type":"Opaque", "stringData":map[string]string{"POSTGRES_PASSWORD":oldPlain},
    })
    proposed, _ := json.Marshal(map[string]interface{}{
        "apiVersion":"v1", "kind":"Secret",
        "metadata":map[string]string{"name":"dev-postgres"},
        "type":"Opaque", "data":map[string]string{"POSTGRES_PASSWORD":proposedB64},
    })
    live, _ := json.Marshal(map[string]interface{}{
        "apiVersion":"v1", "kind":"Secret",
        "metadata":map[string]string{"name":"dev-postgres"},
        "type":"Opaque", "data":map[string]string{"POSTGRES_PASSWORD":liveB64},
    })
    return triple{Old:old, New:proposed, Live:live}
}

func TestSecretExactWriteOnlyRemovalAndSameBytes(t *testing.T) {
    in := secretTriple("before", "YmVmb3Jl", "YmVmb3Jl")
    if !isVerifiedStringDataRemoval(in, []byte(`{"stringData":null}`)) {
        t.Fatal("unchanged secret unexpectedly blocked")
    }
}

func TestSecretPatchWithOtherFieldsIsNotExempt(t *testing.T) {
    in := secretTriple("before", "YmVmb3Jl", "YmVmb3Jl")
    for _, patch := range [][]byte{
        []byte(`{"stringData":{"POSTGRES_PASSWORD":"changed"}}`),
        []byte(`{"stringData":null,"metadata":{"labels":{"new":"x"}}}`),
        []byte(`{"data":null}`),
        []byte(`{}`),
    } {
        if isVerifiedStringDataRemoval(in, patch) {
            t.Fatal("unsafe secret patch was exempted")
        }
    }
}

func TestSecretDifferentBytesRejectsExemption(t *testing.T) {
    for _, in := range []triple{
        secretTriple("before", "YWZ0ZXI=", "YWZ0ZXI="),
        secretTriple("before", "YmVmb3Jl", "YWZ0ZXI="),
        secretTriple("before", "YmVmb3Jl", "@bad-base64"),
    } {
        if isVerifiedStringDataRemoval(in, []byte(`{"stringData":null}`)) {
            t.Fatal("mismatched or malformed credential exempted")
        }
    }
}

func TestSecretTypeMismatchRejectsExemption(t *testing.T) {
    in := secretTriple("before", "YmVmb3Jl", "YmVmb3Jl")
    var live map[string]interface{}
    if err := json.Unmarshal(in.Live, &live); err != nil {t.Fatal(err)}
    live["type"] = "kubernetes.io/dockerconfigjson"
    in.Live, _ = json.Marshal(live)
    if isVerifiedStringDataRemoval(in, []byte(`{"stringData":null}`)) {
        t.Fatal("Secret type change was exempted")
    }
}

func TestSecretDuplicateKeyRepresentationsRejectsExemption(t *testing.T) {
    in := secretTriple("before", "YmVmb3Jl", "YmVmb3Jl")
    var desired map[string]interface{}
    if err := json.Unmarshal(in.New, &desired); err != nil {t.Fatal(err)}
    desired["stringData"] = map[string]interface{}{"POSTGRES_PASSWORD":"before"}
    in.New, _ = json.Marshal(desired)
    if isVerifiedStringDataRemoval(in, []byte(`{"stringData":null}`)) {
        t.Fatal("ambiguous Secret key representations accepted")
    }
}
