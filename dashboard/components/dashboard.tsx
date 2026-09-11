"use client";
import { QueryClient, QueryClientProvider, useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { BarChart, Bar, ResponsiveContainer, XAxis, YAxis, Tooltip } from "recharts";
const query = new QueryClient();
async function load(name: string) { const r = await fetch(`/api/admin/${name}`, { cache: "no-store" }); if (r.status === 401 && typeof window !== "undefined") window.location.replace("/login"); if (!r.ok) throw new Error("Dashboard data is unavailable"); return r.json(); }
function Card({label, value}:{label:string;value:string|number}) { return <section className="card"><span>{label}</span><strong>{value}</strong></section>; }
function Contents() { const [tab, setTab] = useState("teams"); const overview = useQuery({ queryKey:["overview"], queryFn:()=>load("overview") }); const teams = useQuery({ queryKey:["teams"], queryFn:()=>load("teams?limit=25") }); const usage = useQuery({ queryKey:["usage"], queryFn:()=>load("usage?limit=50") }); const prompts = useQuery({ queryKey:["prompts"], queryFn:()=>load("prompt-versions?limit=50") }); if (overview.isLoading) return <main className="shell">Loading dashboard…</main>; if (overview.isError) return <main className="shell">Dashboard data is unavailable.</main>; const o=overview.data; const data=(usage.data?.items||[]).reduce((a:any[], r:any)=>{const e=a.find(x=>x.model===r.model); if(e)e.cost+=Number(r.actual_cost_usd);else a.push({model:r.model,cost:Number(r.actual_cost_usd)});return a;},[]); return <main className="shell"><header><div><h1>ModelBudget</h1><p>Read-only operator dashboard</p></div><button onClick={async()=>{const response=await fetch("/api/session",{method:"DELETE",cache:"no-store"});if(!response.ok)return;query.clear();window.location.replace("/login")}}>Sign out</button></header><div className="cards"><Card label="Active teams" value={o.active_teams}/><Card label="Budget remaining" value={`$${o.current_budget_remaining_usd}`}/><Card label="Successful requests" value={o.usage_succeeded}/><Card label="Failed requests" value={o.usage_failed}/></div><section className="chart"><h2>Cost by model</h2><ResponsiveContainer width="100%" height={230}><BarChart data={data}><XAxis dataKey="model"/><YAxis
  width={110}
  tick={{ fill: "#a6acc8", fontSize: 12 }}
  tickFormatter={(value: number) => value === 0 ? "$0" : "$" + Number(value).toPrecision(3)}
/><Tooltip
  contentStyle={{
    backgroundColor: "#181a27",
    border: "1px solid #454a6a",
    borderRadius: 8,
    color: "#edf0ff"
  }}
  labelStyle={{ color: "#edf0ff" }}
  itemStyle={{ color: "#b5b5ff" }}
  cursor={{ fill: "rgba(119,119,244,0.08)" }}
/><Bar dataKey="cost" fill="#5b5bd6"/></BarChart></ResponsiveContainer></section><nav>{["teams","usage","prompts"].map(x=><button className={tab===x?"active":""} key={x} onClick={()=>setTab(x)}>{x}</button>)}</nav>{tab==="teams"&&<Table rows={teams.data?.items||[]} columns={["name","active","budget_allocated_usd","budget_remaining_usd"]}/>} {tab==="usage"&&<Table rows={usage.data?.items||[]} columns={["team_name","provider","model","status","actual_cost_usd","created_at"]}/>} {tab==="prompts"&&<Table rows={prompts.data?.items||[]} columns={["name","version","status","created_at","approved_at","retired_at"]}/>}</main>; }
function Table({rows,columns}:{rows:any[];columns:string[]}) { return <section className="table"><table><thead><tr>{columns.map(c=><th key={c}>{c.replaceAll("_"," ")}</th>)}</tr></thead><tbody>{rows.map((r,i)=><tr key={i}>{columns.map(c=><td key={c}>{String(r[c]??"—")}</td>)}</tr>)}</tbody></table></section>; }
export default function Dashboard(){return <QueryClientProvider client={query}><Contents/></QueryClientProvider>;}
