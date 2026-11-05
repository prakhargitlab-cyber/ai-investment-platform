package main

import (
    "encoding/json"
    "testing"
    "k8s.io/apimachinery/pkg/util/strategicpatch"
    appsv1 "k8s.io/api/apps/v1"
)

func deploymentTriple(image string) triple {
    body, _ := json.Marshal(map[string]interface{}{
        "apiVersion":"apps/v1", "kind":"Deployment",
        "metadata":map[string]string{"name":"auth-service"},
        "spec":map[string]interface{}{
            "replicas":1,
            "template":map[string]interface{}{
                "metadata":map[string]interface{}{"labels":map[string]string{"app":"auth"}},
                "spec":map[string]interface{}{
                    "containers":[]interface{}{map[string]interface{}{
                        "name":"auth-service", "image":image,
                        "env":[]interface{}{map[string]string{"name":"MODE","value":"safe"}},
                    }},
                },
            },
        },
    })
    return triple{Old:body, New:body, Live:body}
}

func TestDeploymentSpecPatchWithSameImageIsNoop(t *testing.T) {
    tc:=deploymentTriple("same")
    patch:=[]byte(`{"spec":{"template":{"spec":{"containers":[{"name":"auth-service","image":"same"}]}}}}`)
    if !verifiedNoopDeploymentPatch(tc,patch) { t.Fatal("unchanged image should not alter spec") }
}

func TestDeploymentImageChangeCannotBeExempt(t *testing.T) {
    tc:=deploymentTriple("old")
    patch:=[]byte(`{"spec":{"template":{"spec":{"containers":[{"name":"auth-service","image":"new"}]}}}}`)
    if verifiedNoopDeploymentPatch(tc,patch) { t.Fatal("image change was exempted") }
}

func TestDeploymentEnvAndReplicaChangesCannotBeExempt(t *testing.T) {
    tc:=deploymentTriple("same")
    patches:=[]string{
        `{"spec":{"replicas":2}}`,
        `{"spec":{"template":{"spec":{"containers":[{"name":"auth-service","env":[{"name":"MODE","value":"changed"}]}]}}}}`,
        `{"spec":{"template":{"spec":{"containers":[{"name":"auth-service","env":[{"name":"NEW","value":"x"}]}]}}}}`,
        `{"metadata":{"labels":{"x":"changed"}},"spec":{"replicas":1}}`,
    }
    for _,patch:=range patches {
        if verifiedNoopDeploymentPatch(tc,[]byte(patch)) { t.Fatal("unsafe patch exempted") }
    }
}

func TestCalculatedThreeWayPatchDoesNotInventNoop(t *testing.T) {
    tc:=deploymentTriple("old")
    different:=deploymentTriple("new")
    tc.New=different.New
    meta,err:=strategicpatch.NewPatchMetaFromStruct(&appsv1.Deployment{})
    if err!=nil {t.Fatal(err)}
    patch,err:=strategicpatch.CreateThreeWayMergePatch(tc.Old,tc.New,tc.Live,meta,true)
    if err!=nil {t.Fatal(err)}
    if verifiedNoopDeploymentPatch(tc,patch) {t.Fatal("actual image update exempted")}
}
