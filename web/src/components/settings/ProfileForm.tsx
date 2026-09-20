import { useState, useEffect, useRef } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { Loader2, CheckCircle, AlertCircle } from "lucide-react";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { NaicsCombobox } from "@/components/profile/NaicsCombobox";
interface ProfileData {
  company_name: string;
  fiscal_year: number;
  base_currency: string;
  tax_jurisdiction: string;
  business_number: string;
  province: string;
  industry: string;
  naics_code: string;
}

interface ProfileFormProps {
  config: {
    company_name?: string;
    fiscal_year?: number;
    base_currency?: string;
    tax_jurisdiction?: string;
    business_number?: string;
    province?: string;
    industry?: string;
    naics_code?: string;
  } | null;
  isLoading: boolean;
}

const currentYear = new Date().getFullYear();
const yearOptions = [currentYear - 1, currentYear, currentYear + 1];

const INDUSTRY_OPTIONS = [
  "Software & Technology",
  "Consulting & Professional Services",
  "Creative & Design",
  "E-commerce & Retail",
  "Food & Hospitality",
  "Construction & Trades",
  "Health & Wellness",
  "Real Estate",
  "Education & Coaching",
  "Nonprofit",
  "Other",
];

const PROVINCES = [
  "Alberta",
  "British Columbia",
  "Manitoba",
  "New Brunswick",
  "Newfoundland and Labrador",
  "Northwest Territories",
  "Nova Scotia",
  "Nunavut",
  "Ontario",
  "Prince Edward Island",
  "Quebec",
  "Saskatchewan",
  "Yukon",
];

const CURRENCIES = ["CAD", "USD"];

interface FieldErrors {
  company_name?: string;
  business_number?: string;
}

function validateBN(value: string): string | undefined {
  if (!value.trim()) return undefined; // optional field
  // CRA BN format: 9 digits + space + RC + 4 digits (e.g. "123456789 RC0001")
  if (!/^\d{9}\s?RC\d{4}$/i.test(value.trim())) {
    return "Format: 123456789 RC0001";
  }
  return undefined;
}

/** The stored profile as form state, with a default for every field. */
function toFormState(config: ProfileFormProps["config"]): ProfileData {
  return {
    company_name: config?.company_name ?? "",
    fiscal_year: config?.fiscal_year ?? currentYear,
    base_currency: config?.base_currency ?? "CAD",
    tax_jurisdiction: config?.tax_jurisdiction ?? "CA",
    business_number: config?.business_number ?? "",
    province: config?.province ?? "",
    industry: config?.industry ?? "",
    naics_code: config?.naics_code ?? "",
  };
}

export function ProfileForm({ config, isLoading }: ProfileFormProps) {
  const queryClient = useQueryClient();
  const [saved, setSaved] = useState(false);
  const [fieldErrors, setFieldErrors] = useState<FieldErrors>({});
  const [touched, setTouched] = useState<Record<string, boolean>>({});

  // Which fields this session actually edited. Only these may be sent empty:
  // an empty value the user never touched is the form not knowing the answer
  // yet, and submitting it would overwrite what is stored.
  const [edited, setEdited] = useState<Partial<Record<keyof ProfileData, true>>>({});
  const editedRef = useRef(false);

  // Hydrated in the initializer, not in an effect, so the first render of the
  // selects already carries the stored value. A select that renders once with
  // no value and is given one afterwards keeps the empty state it started in.
  const [form, setForm] = useState<ProfileData>(() => toFormState(config));

  // Re-hydrate when the stored profile itself changes — a finished load, or a
  // refetch after a save. Keyed on the content rather than the object identity,
  // because the caller passes a fresh object on every render while the query is
  // still resolving. Unsaved edits win over a refetch.
  const hydratedFrom = useRef(JSON.stringify(config ?? null));
  useEffect(() => {
    const key = JSON.stringify(config ?? null);
    if (key === hydratedFrom.current) return;
    hydratedFrom.current = key;
    if (editedRef.current) return;
    setForm(toFormState(config));
  }, [config]);

  const saveMutation = useMutation({
    mutationFn: () => api.post("/api/onboarding/profile", submittableFields()),
    onSuccess: () => {
      // What was just saved is the new baseline, so a refetch may re-hydrate.
      editedRef.current = false;
      setEdited({});
      queryClient.invalidateQueries({ queryKey: ["onboarding-config"] });
      setSaved(true);
      setTimeout(() => setSaved(false), 3000);
    },
  });

  /** The form minus every field that is empty because it was never edited. */
  function submittableFields(): Partial<ProfileData> {
    const out: Partial<ProfileData> = {};
    for (const key of Object.keys(form) as (keyof ProfileData)[]) {
      const value = form[key];
      if (typeof value === "string" && value.trim() === "" && !edited[key]) continue;
      (out as Record<string, unknown>)[key] = value;
    }
    return out;
  }

  function updateField<K extends keyof ProfileData>(key: K, value: ProfileData[K]) {
    setForm((prev) => ({ ...prev, [key]: value }));
    setEdited((prev) => ({ ...prev, [key]: true }));
    editedRef.current = true;
    setSaved(false);
  }

  if (isLoading) {
    return (
      <Card className="shadow-warm rounded-xl">
        <CardHeader>
          <Skeleton className="h-6 w-48" />
          <Skeleton className="h-4 w-72" />
        </CardHeader>
        <CardContent className="space-y-4">
          {Array.from({ length: 6 }).map((_, i) => (
            <div key={i} className="space-y-2">
              <Skeleton className="h-4 w-32" />
              <Skeleton className="h-10 w-full max-w-md" />
            </div>
          ))}
        </CardContent>
      </Card>
    );
  }

  return (
    <Card className="shadow-warm rounded-xl">
      <CardHeader>
        <CardTitle className="text-xl font-bold">Business Profile</CardTitle>
        <CardDescription>
          Your business details, as they appear on CRA-oriented reports.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            const errors: FieldErrors = {};
            if (!form.company_name.trim()) errors.company_name = "Company name is required.";
            const bnErr = validateBN(form.business_number);
            if (bnErr) errors.business_number = bnErr;
            setFieldErrors(errors);
            setTouched({ company_name: true, business_number: true });
            if (errors.company_name || errors.business_number) return;
            saveMutation.mutate();
          }}
          className="space-y-4 max-w-lg"
          noValidate
        >
          <div className="space-y-2">
            <Label htmlFor="company_name">
              Company Name <span className="text-destructive">*</span>
            </Label>
            <Input
              id="company_name"
              placeholder="Your Business Corp."
              value={form.company_name}
              onChange={(e) => {
                updateField("company_name", e.target.value);
                if (touched.company_name) {
                  setFieldErrors((prev) => ({
                    ...prev,
                    company_name: e.target.value.trim() ? undefined : "Company name is required.",
                  }));
                }
              }}
              onBlur={() => {
                setTouched((prev) => ({ ...prev, company_name: true }));
                setFieldErrors((prev) => ({
                  ...prev,
                  company_name: form.company_name.trim() ? undefined : "Company name is required.",
                }));
              }}
              required
              aria-invalid={!!fieldErrors.company_name && touched.company_name}
              className={`focus-visible:ring-primary ${
                fieldErrors.company_name && touched.company_name ? "border-destructive focus-visible:ring-destructive" : ""
              }`}
            />
            {fieldErrors.company_name && touched.company_name && (
              <p className="flex items-center gap-1 text-sm text-destructive">
                <AlertCircle className="h-3.5 w-3.5 flex-shrink-0" />
                {fieldErrors.company_name}
              </p>
            )}
          </div>

          <div className="space-y-2">
            <Label htmlFor="business_number">CRA Business Number (BN)</Label>
            <Input
              id="business_number"
              placeholder="123456789 RC0001"
              value={form.business_number}
              onChange={(e) => {
                updateField("business_number", e.target.value);
                if (touched.business_number) {
                  setFieldErrors((prev) => ({
                    ...prev,
                    business_number: validateBN(e.target.value),
                  }));
                }
              }}
              onBlur={() => {
                setTouched((prev) => ({ ...prev, business_number: true }));
                setFieldErrors((prev) => ({
                  ...prev,
                  business_number: validateBN(form.business_number),
                }));
              }}
              aria-invalid={!!fieldErrors.business_number && touched.business_number}
              className={`font-mono focus-visible:ring-primary ${
                fieldErrors.business_number && touched.business_number ? "border-destructive focus-visible:ring-destructive" : ""
              }`}
            />
            {fieldErrors.business_number && touched.business_number && (
              <p className="flex items-center gap-1 text-sm text-destructive">
                <AlertCircle className="h-3.5 w-3.5 flex-shrink-0" />
                {fieldErrors.business_number}
              </p>
            )}
          </div>

          <div className="grid grid-cols-2 gap-4">
            <div className="space-y-2">
              <Label htmlFor="fiscal_year">Fiscal Year</Label>
              <Select
                value={String(form.fiscal_year)}
                onValueChange={(v) => updateField("fiscal_year", Number(v))}
              >
                <SelectTrigger id="fiscal_year" className="focus-visible:ring-primary">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {yearOptions.map((year) => (
                    <SelectItem key={year} value={String(year)}>
                      {year}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>

            <div className="space-y-2">
              <Label htmlFor="base_currency">Base Currency</Label>
              <Select
                value={form.base_currency}
                onValueChange={(v) => updateField("base_currency", v)}
              >
                <SelectTrigger id="base_currency" className="font-mono focus-visible:ring-primary">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {CURRENCIES.map((c) => (
                    <SelectItem key={c} value={c}>
                      {c}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          </div>

          <div className="space-y-2">
            <Label htmlFor="province">Province / Territory</Label>
            <Select
              value={form.province ?? ""}
              onValueChange={(v) => updateField("province", v)}
            >
              <SelectTrigger id="province" className="focus-visible:ring-primary">
                <SelectValue placeholder="Select a province" />
              </SelectTrigger>
              <SelectContent>
                {PROVINCES.map((p) => (
                  <SelectItem key={p} value={p}>
                    {p}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          <div className="space-y-2">
            <Label htmlFor="industry">Industry</Label>
            <Select
              value={form.industry ?? ""}
              onValueChange={(v) => updateField("industry", v)}
            >
              <SelectTrigger id="industry" className="focus-visible:ring-primary">
                <SelectValue placeholder="Select an industry" />
              </SelectTrigger>
              <SelectContent>
                {INDUSTRY_OPTIONS.map((opt) => (
                  <SelectItem key={opt} value={opt}>
                    {opt}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          <p className="text-xs text-muted-foreground">
            Your industry powers the peer comparison on the Analytics page.
          </p>

          <div className="space-y-2">
            <Label htmlFor="naics_code">Industry code (NAICS)</Label>
            <NaicsCombobox
              id="naics_code"
              value={form.naics_code}
              onChange={(v) => updateField("naics_code", v)}
            />
            <p className="text-xs text-muted-foreground">
              Optional — a 6-digit NAICS code sharpens your Analytics peer comparison.
            </p>
          </div>

          <div className="space-y-2">
            <Label htmlFor="tax_jurisdiction">Tax Jurisdiction</Label>
            <Input
              id="tax_jurisdiction"
              value={form.tax_jurisdiction === "CA" ? "Canada" : form.tax_jurisdiction}
              disabled
              className="bg-muted/30"
            />
            <p className="text-xs text-muted-foreground">
              Currently limited to Canadian businesses — the tax rules and the account taxonomy are Canadian.
            </p>
          </div>

          <div className="flex items-center gap-3 pt-2">
            <Button
              type="submit"
              disabled={saveMutation.isPending}
              className="bg-primary hover:bg-primary/90"
            >
              {saveMutation.isPending && (
                <Loader2 className="h-4 w-4 mr-1 animate-spin" />
              )}
              Save Profile
            </Button>
            {saved && (
              <span className="flex items-center text-sm text-primary">
                <CheckCircle className="h-4 w-4 mr-1" />
                Saved
              </span>
            )}
            {saveMutation.isError && (
              <span className="text-sm text-rose-600">
                Failed to save. Please try again.
              </span>
            )}
          </div>
        </form>
      </CardContent>
    </Card>
  );
}
