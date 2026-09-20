import { PieChart, Pie, Cell } from "recharts";

interface ResolutionRateDonutProps {
  resolved: number;
  total: number;
  size?: number;
}

export function ResolutionRateDonut({ resolved, total, size = 48 }: ResolutionRateDonutProps) {
  const unresolved = total - resolved;
  const percent = total > 0 ? Math.round((resolved / total) * 100) : 0;

  const data = [
    { name: "Resolved", value: resolved || 0 },
    { name: "Unresolved", value: unresolved || 0 },
  ];

  // Indigo for resolved, muted for unresolved
  const COLORS = ["hsl(var(--primary))", "hsl(var(--border))"];

  if (total === 0) {
    return (
      <div
        className="flex items-center justify-center rounded-full bg-muted"
        style={{ width: size, height: size }}
      >
        <span className="text-xs text-muted-foreground">—</span>
      </div>
    );
  }

  // Size is known and fixed — render PieChart with explicit dimensions
  // instead of ResponsiveContainer. ResponsiveContainer polls its parent for
  // width/height via ResizeObserver; during the first layout pass the parent
  // can briefly report -1, producing a Recharts console warning. `size` is
  // already known here, so the responsive wrapper adds noise without value.
  return (
    <div className="relative" style={{ width: size, height: size }}>
      <PieChart width={size} height={size}>
        <Pie
          data={data}
          cx="50%"
          cy="50%"
          innerRadius={size * 0.33}
          outerRadius={size * 0.48}
          paddingAngle={2}
          dataKey="value"
          strokeWidth={0}
          isAnimationActive={false}
        >
          {data.map((_, index) => (
            <Cell key={`cell-${index}`} fill={COLORS[index]} />
          ))}
        </Pie>
      </PieChart>
      <div className="absolute inset-0 flex items-center justify-center">
        <span className="text-[10px] font-bold text-foreground font-mono">
          {percent}%
        </span>
      </div>
    </div>
  );
}
