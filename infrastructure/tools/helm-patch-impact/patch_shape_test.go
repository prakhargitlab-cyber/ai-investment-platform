package main
import ("reflect"; "testing")
func TestSecretShapeOnlyFixedLabels(t *testing.T) {
 tests := []struct{input, expected string}{
  {`{"stringData":null}`,"STRINGDATA_NULL_ONLY"},
  {`{"stringData":{"key":null}}`,"STRINGDATA_KEY_DELETIONS"},
  {`{"stringData":{"key":"x"}}`,"STRINGDATA_OBJECT_VALUES"},
  {`{"stringData":null,"type":"Opaque"}`,"STRINGDATA_WITH_OTHER_OPERATIONS"},
  {`{"metadata":{"labels":{"key":"x"}}}`,"NO_STRINGDATA_FIELD"},
 }
 for _,tc:=range tests { if got:=secretPatchShape([]byte(tc.input));got!=tc.expected {t.Fatalf("shape %s",got)} }
}
func TestResearchEnvPatchEditVsOrder(t *testing.T) {
 raw:=[]byte(`{"spec":{"template":{"spec":{"containers":[{"name":"research-engine","$setElementOrder/env":[{"name":"other"}],"env":[{"name":"AIP_RESEARCH_EQUITY_MANUAL_EXECUTION_ENABLED","value":"true"},{"name":"other","value":"false"}]}]}}}}`)
 got,err:=researchEnvAudit(raw);if err!=nil {t.Fatal(err)}
 want:=[]string{"APPROVED_ENV_PATCH_ENTRY","ENV_ORDER_DIRECTIVE","UNAPPROVED_ENV_PATCH_ENTRY"}
 if !reflect.DeepEqual(got,want) {t.Fatalf("labels %v",got)}
}
