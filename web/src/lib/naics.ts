/**
 * Curated shortlist of common Canadian solo / SMB 6-digit NAICS codes.
 *
 * Used by the onboarding ProfileStep + Settings ProfileForm NAICS pickers to
 * key the peer-benchmark ("reasonableness") comparison to the right industry
 * cohort. Not exhaustive — the field accepts any 2–6 digit code; this is just
 * a friendly autocomplete for the most common cases.
 */
export interface NaicsOption {
  code: string;
  label: string;
}

export const NAICS_OPTIONS: NaicsOption[] = [
  { code: "541510", label: "Computer systems design & related services" },
  { code: "541511", label: "Custom computer programming services" },
  { code: "541512", label: "Computer systems design services" },
  { code: "541611", label: "Management consulting services" },
  { code: "541618", label: "Other management consulting services" },
  { code: "541620", label: "Environmental consulting services" },
  { code: "541690", label: "Other scientific & technical consulting" },
  { code: "541810", label: "Advertising agencies" },
  { code: "541820", label: "Public relations services" },
  { code: "541430", label: "Graphic design services" },
  { code: "541410", label: "Interior design services" },
  { code: "541310", label: "Architectural services" },
  { code: "541330", label: "Engineering services" },
  { code: "541110", label: "Offices of lawyers" },
  { code: "541212", label: "Offices of accountants / bookkeeping" },
  { code: "541990", label: "Other professional & technical services" },
  { code: "454110", label: "Electronic shopping & mail-order (e-commerce)" },
  { code: "448140", label: "Family clothing stores / retail" },
  { code: "722511", label: "Full-service restaurants" },
  { code: "722513", label: "Limited-service eating places" },
  { code: "236110", label: "Residential building construction" },
  { code: "238220", label: "Plumbing, heating & air-conditioning" },
  { code: "238210", label: "Electrical contractors" },
  { code: "621110", label: "Offices of physicians" },
  { code: "621210", label: "Offices of dentists" },
  { code: "621310", label: "Offices of chiropractors" },
  { code: "621340", label: "Physical / occupational therapists" },
  { code: "812115", label: "Hair & personal-care services" },
  { code: "611620", label: "Sports & recreation instruction / coaching" },
  { code: "711510", label: "Independent artists, writers & performers" },
  { code: "531210", label: "Offices of real estate agents & brokers" },
  { code: "484110", label: "General freight trucking, local" },
  { code: "561730", label: "Landscaping services" },
  { code: "541922", label: "Commercial photography" },
];
