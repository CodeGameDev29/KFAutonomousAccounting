import { useNavigate } from "react-router-dom";
import { BookOpen, ArrowLeft } from "lucide-react";
import { Button } from "@/components/ui/button";

export function NotFound() {
  const navigate = useNavigate();

  return (
    <div className="flex min-h-screen flex-col items-center justify-center px-4 bg-background">
      <div className="mx-auto flex h-20 w-20 items-center justify-center rounded-2xl bg-primary/10">
        <BookOpen className="h-10 w-10 text-primary" />
      </div>
      <h1 className="mt-6 text-5xl font-bold tracking-tight">404</h1>
      <p className="mt-2 text-lg text-muted-foreground">Page not found</p>
      <p className="mt-1 text-sm text-muted-foreground">
        The page you are looking for does not exist or has been moved.
      </p>
      <div className="mt-8 flex gap-4">
        <Button
          variant="outline"
          onClick={() => navigate(-1)}
          className="border-primary/20 text-primary hover:bg-primary/5"
        >
          <ArrowLeft className="mr-2 h-4 w-4" />
          Go back
        </Button>
        <Button
          onClick={() => navigate("/")}
          className="bg-primary hover:bg-primary/90"
        >
          Return home
        </Button>
      </div>
    </div>
  );
}
