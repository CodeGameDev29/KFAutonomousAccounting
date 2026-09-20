import { useState, useEffect, useRef } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { NaicsCombobox } from "@/components/profile/NaicsCombobox";
import { ArrowRight, Building2, ChevronDown, ChevronUp } from "lucide-react";

interface ProfileDefaultValues {
  company_name?: string;
  fiscal_year?: number;
  base_currency?: string;
  province?: string;
  industry?: string;
  business_summary?: string;
  naics_code?: string;
}

interface ProfileStepProps {
  onNext: (data: {
    company_name: string;
    fiscal_year: number;
    base_currency: string;
    province: string;
    industry: string;
    business_summary: string;
    naics_code: string;
  }) => void;
  isSubmitting: boolean;
  defaultValues?: ProfileDefaultValues;
}

const CANADIAN_PROVINCES = [
  { value: "", label: "Select a province" },
  { value: "Alberta", label: "Alberta" },
  { value: "British Columbia", label: "British Columbia" },
  { value: "Manitoba", label: "Manitoba" },
  { value: "New Brunswick", label: "New Brunswick" },
  { value: "Newfoundland and Labrador", label: "Newfoundland and Labrador" },
  { value: "Nova Scotia", label: "Nova Scotia" },
  { value: "Northwest Territories", label: "Northwest Territories" },
  { value: "Nunavut", label: "Nunavut" },
  { value: "Ontario", label: "Ontario" },
  { value: "Prince Edward Island", label: "Prince Edward Island" },
  { value: "Quebec", label: "Quebec" },
  { value: "Saskatchewan", label: "Saskatchewan" },
  { value: "Yukon", label: "Yukon" },
] as const;

const INDUSTRY_OPTIONS = [
  { value: "", label: "Select an industry" },
  { value: "Software & Technology", label: "Software & Technology" },
  { value: "Consulting & Professional Services", label: "Consulting & Professional Services" },
  { value: "Creative & Design", label: "Creative & Design" },
  { value: "E-commerce & Retail", label: "E-commerce & Retail" },
  { value: "Food & Hospitality", label: "Food & Hospitality" },
  { value: "Construction & Trades", label: "Construction & Trades" },
  { value: "Health & Wellness", label: "Health & Wellness" },
  { value: "Real Estate", label: "Real Estate" },
  { value: "Education & Coaching", label: "Education & Coaching" },
  { value: "Nonprofit", label: "Nonprofit" },
  { value: "Other", label: "Other" },
] as const;

const currentYear = new Date().getFullYear();
const yearOptions = [currentYear - 1, currentYear, currentYear + 1];

export function ProfileStep({ onNext, isSubmitting, defaultValues }: ProfileStepProps) {
  const [companyName, setCompanyName] = useState(defaultValues?.company_name ?? "");
  const [fiscalYear, setFiscalYear] = useState(defaultValues?.fiscal_year ?? currentYear);
  const [baseCurrency, setBaseCurrency] = useState(defaultValues?.base_currency ?? "CAD");
  const [province, setProvince] = useState(defaultValues?.province ?? "");
  const [industry, setIndustry] = useState(defaultValues?.industry ?? "");
  const [businessSummary, setBusinessSummary] = useState(defaultValues?.business_summary ?? "");
  const [naicsCode, setNaicsCode] = useState(defaultValues?.naics_code ?? "");
  const [showOptional, setShowOptional] = useState(false);

  // Populate from late-arriving saved state (query resolves after mount)
  const appliedDefaults = useRef(false);
  useEffect(() => {
    if (defaultValues && !appliedDefaults.current) {
      appliedDefaults.current = true;
      if (defaultValues.company_name) setCompanyName(defaultValues.company_name);
      if (defaultValues.fiscal_year) setFiscalYear(defaultValues.fiscal_year);
      if (defaultValues.base_currency) setBaseCurrency(defaultValues.base_currency);
      if (defaultValues.province) setProvince(defaultValues.province);
      if (defaultValues.industry) setIndustry(defaultValues.industry);
      if (defaultValues.business_summary) setBusinessSummary(defaultValues.business_summary);
      if (defaultValues.naics_code) setNaicsCode(defaultValues.naics_code);
    }
  }, [defaultValues]);

  const canProceed = companyName.trim().length > 0;

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!canProceed) return;
    onNext({
      company_name: companyName.trim(),
      fiscal_year: fiscalYear,
      base_currency: baseCurrency,
      province,
      industry,
      business_summary: businessSummary.trim(),
      naics_code: naicsCode,
    });
  };

  return (
    <form onSubmit={handleSubmit} className="space-y-6">
      <div className="mx-auto mb-2 flex h-14 w-14 items-center justify-center rounded-2xl bg-primary/10">
        <Building2 className="h-7 w-7 text-primary" />
      </div>
      <div className="text-center">
        <h2 className="text-2xl font-bold tracking-tight">What's your business called?</h2>
        <p className="mt-1 text-sm text-muted-foreground">
          A short description lets the app choose sensible expense categories
          for your business.
        </p>
      </div>

      <div className="space-y-4">
        <div className="space-y-2">
          <Label htmlFor="company-name">Company Name</Label>
          <Input
            id="company-name"
            placeholder="e.g. Maple Leaf Consulting Inc."
            value={companyName}
            onChange={(e) => setCompanyName(e.target.value)}
            className="focus-visible:ring-primary"
            autoFocus
          />
        </div>

        <div className="space-y-2">
          <Label htmlFor="province">Province</Label>
          <select
            id="province"
            value={province}
            onChange={(e) => setProvince(e.target.value)}
            className="flex h-10 w-full rounded-md border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-2"
          >
            {CANADIAN_PROVINCES.map((p) => (
              <option key={p.value} value={p.value}>
                {p.label}
              </option>
            ))}
          </select>
          <p className="text-xs text-muted-foreground">
            This sets the GST/HST rate used in your reports.
          </p>
        </div>

        {/* Collapsible optional fields */}
        <button
          type="button"
          onClick={() => setShowOptional(!showOptional)}
          className="flex items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground transition-colors"
        >
          {showOptional ? <ChevronUp className="h-4 w-4" /> : <ChevronDown className="h-4 w-4" />}
          Optional details
        </button>

        {showOptional && (
          <div className="space-y-4 animate-in slide-in-from-top-2 duration-200">
            <div className="space-y-2">
              <Label htmlFor="industry">Industry</Label>
              <select
                id="industry"
                value={industry}
                onChange={(e) => setIndustry(e.target.value)}
                className="flex h-10 w-full rounded-md border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-2"
              >
                {INDUSTRY_OPTIONS.map((opt) => (
                  <option key={opt.value} value={opt.value}>
                    {opt.label}
                  </option>
                ))}
              </select>
            </div>

            <div className="space-y-2">
              <Label htmlFor="naics-code">Your industry (NAICS) — powers the peer comparison</Label>
              <NaicsCombobox id="naics-code" value={naicsCode} onChange={setNaicsCode} />
              <p className="text-xs text-muted-foreground">
                Optional. A precise code gives you a sharper comparison to
                similar Canadian businesses.
              </p>
            </div>

            <div className="space-y-2">
              <Label htmlFor="business-summary">What does your business do?</Label>
              <textarea
                id="business-summary"
                placeholder="e.g. We run a small bakery with two locations. Our main costs are ingredients, rent, and part-time staff."
                value={businessSummary}
                onChange={(e) => setBusinessSummary(e.target.value)}
                rows={4}
                className="flex w-full rounded-md border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-2"
              />
              <p className="text-xs text-muted-foreground">
                2-4 sentences. This helps the app recognise your typical
                expenses and categorize them accurately.
              </p>
            </div>

            <div className="space-y-2">
              <Label htmlFor="fiscal-year">Year to track</Label>
              <select
                id="fiscal-year"
                value={fiscalYear}
                onChange={(e) => setFiscalYear(Number(e.target.value))}
                className="flex h-10 w-full rounded-md border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-2"
              >
                {yearOptions.map((year) => (
                  <option key={year} value={year}>
                    {year}
                  </option>
                ))}
              </select>
              <p className="text-xs text-muted-foreground">
                Which year's bookkeeping do you want to set up?
              </p>
            </div>

            <div className="space-y-2">
              <Label htmlFor="base-currency">Main currency</Label>
              <select
                id="base-currency"
                value={baseCurrency}
                onChange={(e) => setBaseCurrency(e.target.value)}
                className="flex h-10 w-full rounded-md border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-2"
              >
                <option value="CAD">CAD - Canadian Dollar</option>
                <option value="USD">USD - US Dollar</option>
              </select>
            </div>
          </div>
        )}
      </div>

      <Button
        type="submit"
        className="w-full gap-2 bg-primary hover:bg-primary/90"
        disabled={!canProceed || isSubmitting}
      >
        {isSubmitting ? "Saving..." : "Next"}
        <ArrowRight className="h-4 w-4" />
      </Button>
    </form>
  );
}
