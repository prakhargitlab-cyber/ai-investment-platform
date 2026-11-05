package main

import (
    "reflect"
    "testing"
)

func TestJWTFailureCodeForMismatchedBytes(t *testing.T) {
    input := secretTriple("before", "YmVmb3Jl", "YWZ0ZXI=")
    code := secretNoopFailure(input, []byte(`{"stringData":null}`))
    if code != "NEW_TO_LIVE_BYTES_MISMATCH" { t.Fatalf("unexpected code: %s", code) }
}

func TestJWTFailureCodeForChangedDesiredBytes(t *testing.T) {
    input := secretTriple("before", "YWZ0ZXI=", "YWZ0ZXI=")
    code := secretNoopFailure(input, []byte(`{"stringData":null}`))
    if code != "OLD_TO_NEW_BYTES_MISMATCH" { t.Fatalf("unexpected code: %s", code) }
}

func TestJWTFailureCodeForPatchShape(t *testing.T) {
    input := secretTriple("before", "YmVmb3Jl", "YmVmb3Jl")
    if secretNoopFailure(input, []byte(`{"data":{"x":null}}`)) != "PATCH_SHAPE" {
        t.Fatal("unexpected JWT failure code")
    }
}

func TestJWTVerifiedCode(t *testing.T) {
    input := secretTriple("before", "YmVmb3Jl", "YmVmb3Jl")
    if secretNoopFailure(input, []byte(`{"stringData":null}`)) != "VERIFIED" {
        t.Fatal("matching secret was not verified")
    }
}

func TestResearchPatchLabelsNoSecretLeak(t *testing.T) {
    raw := []byte(`{"spec":{"template":{"spec":{"containers":[{"name":"research-engine","env":[{"name":"AIP_RESEARCH_EQUITY_MANUAL_EXECUTION_ENABLED","value":"sensitive"},{"name":"UNKNOWN","value":"another-sensitive"}]}]}}}}`)
    labels, err := researchPatchLabels(raw)
    if err != nil { t.Fatal(err) }
    want := []string{"APPROVED_ENV_NAME", "ENV_VALUE_PATCH", "OTHER_ENV_NAME"}
    if !reflect.DeepEqual(labels, want) { t.Fatalf("unexpected labels: %v", labels) }
    for _, label := range labels {
        if label == "sensitive" || label == "another-sensitive" || label == "UNKNOWN" {
            t.Fatal("research patch leaked raw data")
        }
    }
}

func TestResearchPatchUnexpectedContainerFields(t *testing.T) {
    labels, err := researchPatchLabels([]byte(`{"spec":{"template":{"spec":{"containers":[{"name":"research-engine","resources":{}}],"terminationGracePeriodSeconds":30}}}}`))
    if err != nil {t.Fatal(err)}
    want := []string{"OTHER_CONTAINER_FIELD","OTHER_POD_SPEC"}
    if !reflect.DeepEqual(labels,want) {t.Fatalf("unexpected labels: %v",labels)}
}

func TestResearchPatchDirectiveIsNotIgnored(t *testing.T) {
    labels, err := researchPatchLabels([]byte(`{"spec":{"template":{"spec":{"$setElementOrder/containers":[{"name":"research-engine"}]}}}}`))
    if err != nil {t.Fatal(err)}
    if !reflect.DeepEqual(labels, []string{"STRATEGIC_LIST_DIRECTIVE"}) {
        t.Fatalf("unexpected labels: %v",labels)
    }
}
