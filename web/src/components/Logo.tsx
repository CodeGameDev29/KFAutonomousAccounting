import { cn } from "@/lib/utils";

interface LogoProps {
  size?: "sm" | "md" | "lg";
  className?: string;
}

export function Logo({ size = "md", className }: LogoProps) {
  const sizeMap = { sm: "h-6 w-6", md: "h-8 w-8", lg: "h-10 w-10" };
  return (
    <img
      src="/logo.svg"
      alt="Autonomous Accounting"
      className={cn(sizeMap[size], className)}
    />
  );
}
