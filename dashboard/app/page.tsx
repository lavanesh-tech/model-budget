import { redirect } from "next/navigation";
import { hasSession } from "@/lib/session";
import Dashboard from "@/components/dashboard";
export default async function Page() { if (!(await hasSession())) redirect("/login"); return <Dashboard />; }
