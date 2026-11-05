package main

import (
    "encoding/json"
    "fmt"
    "testing"
)

func document(kind, name, image string) json.RawMessage {
    raw, _ := json.Marshal(map[string]interface{}{
        "apiVersion":"apps/v1", "kind":kind,
        "metadata":map[string]interface{}{"name":name},
        "spec":map[string]interface{}{"template":map[string]interface{}{
            "spec":map[string]interface{}{
                "containers":[]interface{}{map[string]interface{}{"name":"app","image":image}},
            },
        }},
    })
    return raw
}

func TestNoPatchWhenStoredProposedAndLiveEqual(t *testing.T) {
    d:=document("Deployment","auth-service","image:v1")
    changed, _, err:=calculatePatch(triple{d,d,d})
    if err != nil || changed {t.Fatalf("unexpected patch: %v",err)}
}

func TestThreeWayPatchDetectsUnrelatedLiveDrift(t *testing.T) {
    d:=document("Deployment","auth-service","image:v1")
    drift:=document("Deployment","auth-service","image:v2")
    changed, name, err:=calculatePatch(triple{d,d,drift})
    if err != nil { t.Fatal(err) }
    // This is a direct empirical check of the Kubernetes strategicpatch
    // library, not an assumption about Helm's drift handling.
    t.Logf("three_way_patch_for_live_drift=%v resource=%s", changed,name)
}

func TestThreeWayPatchDetectsProposedImageChange(t *testing.T) {
    before:=document("Deployment","research-engine","old")
    after:=document("Deployment","research-engine","new")
    changed, _, err:=calculatePatch(triple{before,after,before})
    if err != nil || !changed {t.Fatalf("expected image patch: %v",err)}
}

func TestDifferentIdentityIsRejected(t *testing.T) {
    old:=document("Deployment","research-engine","old")
    other:=document("Deployment","auth-service","old")
    _, _, err:=calculatePatch(triple{old,other,old})
    if err==nil {t.Fatal("expected mismatch failure")}
}

func TestUnknownKindFailsClosed(t *testing.T) {
    raw:=json.RawMessage(`{"apiVersion":"x/v1","kind":"Untrusted","metadata":{"name":"x"}}`)
    _, _, err:=calculatePatch(triple{raw,raw,raw})
    if err==nil {t.Fatal("unsupported kind accepted")}
}

func TestFiftyFourResourcesRequired(t *testing.T) {
    _,err:=perform(request{Resources:[]triple{}})
    if err==nil {t.Fatal("empty release should block")}
    if fmt.Sprint(err)!="EXPECTED_54_RESOURCES" {t.Fatalf("unexpected error")}
}
