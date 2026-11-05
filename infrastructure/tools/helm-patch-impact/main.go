// helm-patch-impact calculates Kubernetes strategic three-way patches entirely
// in memory using the same strategicpatch API Helm calls for built-in resources.
// It never prints patch contents, values, resource bodies, or credentials.
// It is a release-gate diagnostic, not a Helm client and never writes to API.
package main

import (
    "bytes"
    "encoding/json"
    "fmt"
    "io"
    "os"
    "sort"

    appsv1 "k8s.io/api/apps/v1"
    corev1 "k8s.io/api/core/v1"
    networkingv1 "k8s.io/api/networking/v1"
    rbacv1 "k8s.io/api/rbac/v1"
    "k8s.io/apimachinery/pkg/util/strategicpatch"
)

type resource struct {
    APIVersion string `json:"apiVersion"`
    Kind string `json:"kind"`
    Metadata struct { Name string `json:"name"` } `json:"metadata"`
}

type triple struct {
    Old json.RawMessage `json:"old"`
    New json.RawMessage `json:"new"`
    Live json.RawMessage `json:"live"`
}

type request struct {
    Resources []triple `json:"resources"`
}

type report struct {
    Resources int `json:"resources"`
    NonEmpty int `json:"nonempty"`
    RiskNames []string `json:"risk_names"`
    VerifiedSecretNoop []string `json:"verified_secret_noop"`
    VerifiedDeploymentNoop []string `json:"verified_deployment_noop"`
    PatchCategories map[string][]string `json:"patch_categories"`
    ResearchPatch bool `json:"research_patch"`
    JWTFailureCode string `json:"jwt_failure_code"`
    JWTPatchShape string `json:"jwt_patch_shape"`
    ResearchPatchLabels []string `json:"research_patch_labels"`
    ResearchEnvAudit []string `json:"research_env_audit"`
    Blocked bool `json:"blocked"`
    Version string `json:"version"`
}

func patchStructure(version, kind string) (interface{}, error) {
    switch version + "/" + kind {
    case "apps/v1/Deployment": return &appsv1.Deployment{}, nil
    case "apps/v1/StatefulSet": return &appsv1.StatefulSet{}, nil
    case "v1/Secret": return &corev1.Secret{}, nil
    case "v1/ConfigMap": return &corev1.ConfigMap{}, nil
    case "v1/Service": return &corev1.Service{}, nil
    case "v1/ServiceAccount": return &corev1.ServiceAccount{}, nil
    case "v1/PersistentVolumeClaim": return &corev1.PersistentVolumeClaim{}, nil
    case "networking.k8s.io/v1/Ingress": return &networkingv1.Ingress{}, nil
    case "rbac.authorization.k8s.io/v1/Role": return &rbacv1.Role{}, nil
    case "rbac.authorization.k8s.io/v1/RoleBinding": return &rbacv1.RoleBinding{}, nil
    default: return nil, fmt.Errorf("UNSUPPORTED_KUBERNETES_KIND")
    }
}

func calculatePatch(t triple) (bool, string, error) {
    changed, name, _, _, _, err := calculatePatchDetailed(t)
    return changed, name, err
}

func calculatePatchDetailed(t triple) (bool, string, []string, bool, []byte, error) {
    var o, n, l resource
    if err := json.Unmarshal(t.Old, &o); err != nil { return false, "", nil, false, nil, err }
    if err := json.Unmarshal(t.New, &n); err != nil { return false, "", nil, false, nil, err }
    if err := json.Unmarshal(t.Live, &l); err != nil { return false, "", nil, false, nil, err }
    if o.Kind == "" || o.Metadata.Name == "" ||
        o != n || o != l {
        return false, "", nil, false, nil, fmt.Errorf("RESOURCE_IDENTITY_MISMATCH")
    }
    obj, err := patchStructure(o.APIVersion, o.Kind)
    if err != nil { return false, "", nil, false, nil, err }
    meta, err := strategicpatch.NewPatchMetaFromStruct(obj)
    if err != nil { return false, "", nil, false, nil, err }
    // Helm's built-in object path: CreateThreeWayMergePatch(oldData, newData,
    // currentData, patchMeta, true). This is a calculation, not an API call.
    patch, err := strategicpatch.CreateThreeWayMergePatch(
        t.Old, t.New, t.Live, meta, true)
    if err != nil { return false, "", nil, false, nil, err }
    trimmed := bytes.TrimSpace(patch)
    if len(trimmed) == 0 { return false, "", nil, false, nil, fmt.Errorf("EMPTY_PATCH_RESPONSE") }
    if bytes.Equal(trimmed, []byte("{}")) { return false, o.Kind + "/" + o.Metadata.Name, []string{}, false, nil, nil }
    categories, err := classifyPatch(o.Kind, patch)
    if err != nil { return false, "", nil, false, nil, err }
        // Only the three reviewed release Secrets may receive a no-op exemption.
    allowedSecret := o.Kind == "Secret" && (
        o.Metadata.Name == "aip-auth-jwt" ||
        o.Metadata.Name == "dev-postgres" ||
        o.Metadata.Name == "dev-searxng")
    verifiedNoop := allowedSecret && isVerifiedStringDataRemoval(t, patch)
    if o.Kind == "Secret" && o.Metadata.Name == "aip-auth-jwt" &&
        !verifiedNoop {
        verifiedNoop = isVerifiedLiveJWTDataRewrite(t, patch)
    }
    return true, o.Kind + "/" + o.Metadata.Name, categories, verifiedNoop, patch, nil
}

func perform(r request) (report, error) {
    out := report{Resources:len(r.Resources), RiskNames:[]string{}, VerifiedSecretNoop:[]string{}, VerifiedDeploymentNoop:[]string{}, PatchCategories:map[string][]string{},Version:"strategic-three-way-v1"}
    if len(r.Resources) != 54 { return out, fmt.Errorf("EXPECTED_54_RESOURCES") }
    seen := map[string]bool{}
    for _, t := range r.Resources {
        changed, name, categories, secretNoop, patch, err := calculatePatchDetailed(t)
        if err != nil { return out, err }
        if seen[name] { return out, fmt.Errorf("DUPLICATE_RESOURCE") }
        seen[name] = true
        if !changed { continue }
        out.NonEmpty++
        out.PatchCategories[name] = categories
        if name == "Secret/aip-auth-jwt" {
            out.JWTPatchShape = secretPatchShape(patch)
            out.JWTFailureCode = secretNoopFailure(t, patch)
            if secretNoop && out.JWTFailureCode != "VERIFIED" {
                if !isVerifiedLiveJWTDataRewrite(t, patch) {
                    return out, fmt.Errorf("SECRET_AUDIT_INCONSISTENT")
                }
                out.JWTFailureCode = "VERIFIED_LIVE_DATA_REWRITE"
            }
            if (out.JWTFailureCode == "VERIFIED" ||
                out.JWTFailureCode == "VERIFIED_LIVE_DATA_REWRITE") != secretNoop {
                return out, fmt.Errorf("SECRET_AUDIT_INCONSISTENT")
            }
        }
        if name == "Deployment/research-engine" {
            out.ResearchPatchLabels, err = researchPatchLabels(patch)
            if err != nil { return out, err }
            out.ResearchEnvAudit, err = researchEnvAudit(patch)
            if err != nil { return out, err }
        }
        if secretNoop {
            out.VerifiedSecretNoop = append(out.VerifiedSecretNoop, name)
        } else if name == "Deployment/research-engine" {
            out.ResearchPatch = true
        } else if o := name; len(o) > len("Deployment/") &&
            o[:len("Deployment/")] == "Deployment/" &&
            verifiedNoopDeploymentPatch(t, patch) {
            out.VerifiedDeploymentNoop = append(out.VerifiedDeploymentNoop, name)
        } else {
            out.RiskNames = append(out.RiskNames, name)
        }
    }
    sort.Strings(out.RiskNames)
    sort.Strings(out.VerifiedSecretNoop)
    sort.Strings(out.VerifiedDeploymentNoop)
    // Verified write-only Secret stringData removals do not affect stored
    // bytes. Research patch still requires separate field-level approval.
    out.Blocked = len(out.RiskNames) != 0 || out.ResearchPatch
    return out, nil
}

func main() {
    // One bounded JSON stream over stdin; avoid command-line parameters with
    // Secret values. No network or Kubernetes API interaction.
    input, err := io.ReadAll(io.LimitReader(os.Stdin, 16*1024*1024+1))
    if err != nil || len(input) > 16*1024*1024 {
        fmt.Fprintln(os.Stderr, "PATCH_INPUT_BLOCKED")
        os.Exit(2)
    }
    var req request
    if err = json.Unmarshal(input, &req); err != nil {
        fmt.Fprintln(os.Stderr, "PATCH_INPUT_INVALID")
        os.Exit(2)
    }
    report, err := perform(req)
    if err != nil {
        // Never echo errors that might include sensitive values.
        fmt.Fprintln(os.Stderr, "PATCH_CALCULATION_BLOCKED")
        os.Exit(2)
    }
    if err := json.NewEncoder(os.Stdout).Encode(report); err != nil {
        os.Exit(2)
    }
    if report.Blocked { os.Exit(3) }
}
