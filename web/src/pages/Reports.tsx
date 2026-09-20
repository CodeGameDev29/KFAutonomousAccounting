import { useParams, useNavigate } from "react-router-dom";
import { YearIndex } from "@/components/reports/YearIndex";
import { MonthGrid } from "@/components/reports/MonthGrid";
import { AccountDetail } from "@/components/reports/AccountDetail";
import { Breadcrumbs } from "@/components/shared/Breadcrumbs";

function ReportsBreadcrumb({ year, month }: { year?: string; month?: string }) {
  const navigate = useNavigate();
  if (!year) return null;

  const monthName = month
    ? new Date(parseInt(year), parseInt(month) - 1).toLocaleString("en-CA", { month: "long" })
    : null;

  const segments = [
    { label: "Reports", onClick: () => navigate("/reports") },
    ...(month
      ? [
          { label: year, onClick: () => navigate(`/reports/${year}`) },
          { label: monthName! },
        ]
      : [{ label: year }]),
  ];

  return <Breadcrumbs segments={segments} />;
}

export function Reports() {
  const { year, month } = useParams<{ year?: string; month?: string }>();

  const content = (() => {
    // Level 3: Year + Month → Account detail with transaction table
    if (year && month) {
      const y = parseInt(year, 10);
      const m = parseInt(month, 10);
      if (!isNaN(y) && !isNaN(m) && m >= 1 && m <= 12) {
        return <AccountDetail year={y} month={m} />;
      }
    }

    // Level 2: Year only → 12-month grid
    if (year) {
      const y = parseInt(year, 10);
      if (!isNaN(y)) {
        return <MonthGrid year={y} />;
      }
    }

    // Level 1: No params → Year index
    return <YearIndex />;
  })();

  return (
    <div className="space-y-4">
      <ReportsBreadcrumb year={year} month={month} />
      {content}
    </div>
  );
}
