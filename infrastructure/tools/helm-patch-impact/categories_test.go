package main

import (
    "encoding/json"
    "reflect"
    "testing"
)

func TestPatchCategoriesAreFixedLabels(t *testing.T) {
    raw := []byte(`{"spec":{"template":{"spec":{"containers":[{"name":"api","image":"SECRET-HIDDEN","env":[{"name":"KEY","value":"SECRET-HIDDEN"}]}]}}}}`)
    got, err := classifyPatch("Deployment", raw)
    if err != nil { t.Fatal(err) }
    want := []string{"container-env","container-image"}
    if !reflect.DeepEqual(got, want) { t.Fatalf("wrong categories: %v", got) }
    b, _ := json.Marshal(got)
    if string(b) == "SECRET-HIDDEN" { t.Fatal("exposed value") }
}

func TestSecretPatchCategoriesNeverExposeValues(t *testing.T) {
    raw := []byte(`{"data":{"POSTGRES_PASSWORD":"c2Vuc2l0aXZl"},"metadata":{"annotations":{"private":"secret"}}}`)
    got, err := classifyPatch("Secret", raw)
    if err != nil { t.Fatal(err) }
    want := []string{"metadata", "secret-data-or-config-data"}
    if !reflect.DeepEqual(got, want) { t.Fatalf("wrong categories: %v", got) }
}

func TestSecretStringDataRepresentationIsClassified(t *testing.T) {
    got, err := classifyPatch("Secret", []byte(`{"stringData":null}`))
    if err != nil { t.Fatal(err) }
    if !reflect.DeepEqual(got, []string{"secret-stringData"}) { t.Fatalf("%v", got) }
}
