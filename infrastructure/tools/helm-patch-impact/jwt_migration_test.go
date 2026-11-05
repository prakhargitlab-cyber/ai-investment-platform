package main

import (
    "encoding/json"
    "testing"
)

func jwtMigrationTriple(oldValue, currentValue string) triple {
    old, _ := json.Marshal(map[string]interface{}{
        "apiVersion":"v1", "kind":"Secret",
        "metadata":map[string]string{"name":"aip-auth-jwt"},
        "type":"Opaque",
        "stringData":map[string]string{"jwt-secret":oldValue},
    })
    newManifest, _ := json.Marshal(map[string]interface{}{
        "apiVersion":"v1", "kind":"Secret",
        "metadata":map[string]string{"name":"aip-auth-jwt"},
        "type":"Opaque",
        "data":map[string]string{"jwt-secret":currentValue},
    })
    live, _ := json.Marshal(map[string]interface{}{
        "apiVersion":"v1", "kind":"Secret",
        "metadata":map[string]string{"name":"aip-auth-jwt"},
        "type":"Opaque",
        "data":map[string]string{"jwt-secret":currentValue},
    })
    return triple{Old:old, New:newManifest, Live:live}
}

func TestJWTMigrationNoopMatchesLiveBytesEvenIfOldStale(t *testing.T) {
    input := jwtMigrationTriple("old-ephemeral", "Y3VycmVudA==")
    patch := []byte(`{"stringData":null,"data":{"jwt-secret":"Y3VycmVudA=="}}`)
    if !isVerifiedLiveJWTDataRewrite(input, patch) {
        t.Fatal("exact live-preserving JWT representation conversion rejected")
    }
    if !isVerifiedLiveJWTDataRewrite(input, []byte(`{"stringData":null}`)) {
        t.Fatal("JWT no-op removal without redundant data field rejected")
    }
}

func TestJWTMigrationRejectsAnyCredentialModification(t *testing.T) {
    input := jwtMigrationTriple("old", "Y3VycmVudA==")
    bad := []string{
        `{"stringData":null,"data":{"jwt-secret":"Y2hhbmdlZA=="}}`,
        `{"stringData":{"jwt-secret":"replace"},"data":{"jwt-secret":"Y3VycmVudA=="}}`,
        `{"stringData":null,"data":{"jwt-secret":null}}`,
        `{"stringData":null,"data":{"other":"Y3VycmVudA=="}}`,
        `{"stringData":null,"data":{"jwt-secret":"Y3VycmVudA=="},"metadata":{"labels":{"x":"y"}}}`,
    }
    for _, p := range bad {
        if isVerifiedLiveJWTDataRewrite(input, []byte(p)) {
            t.Fatal("unsafe JWT patch was exempted")
        }
    }
}

func TestJWTMigrationRejectsChangedLiveSecret(t *testing.T) {
    input := jwtMigrationTriple("old", "Y3VycmVudA==")
    var live map[string]interface{}
    if err := json.Unmarshal(input.Live, &live); err != nil {t.Fatal(err)}
    live["data"] = map[string]string{"jwt-secret":"Y2hhbmdlZA=="}
    input.Live, _ = json.Marshal(live)
    if isVerifiedLiveJWTDataRewrite(
        input, []byte(`{"stringData":null,"data":{"jwt-secret":"Y3VycmVudA=="}}`)) {
        t.Fatal("divergent live JWT accepted")
    }
}
