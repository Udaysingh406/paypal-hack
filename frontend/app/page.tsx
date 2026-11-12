"use client";

import { FormEvent, useEffect, useMemo, useState } from "react";

type Group = { id: string; name: string };
type Member = { id: string; display_name: string };
type Expense = { id: string; description: string; amount_minor: number; currency: string; paid_by_id: string; status: string };
type Balance = { currency: string | null; balances: Record<string, number> };
type Transfer = { id: string; from_user_id: string; to_user_id: string; amount_minor: number; status: string };
type Settlement = { id: string; status: string; transfers: Transfer[] };
type GroupDetail = { id: string; name: string; members: Member[] };

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export default function Home() {
  const [token, setToken] = useState("");
  const [userId, setUserId] = useState("");
  const [groups, setGroups] = useState<Group[]>([]);
  const [selectedGroup, setSelectedGroup] = useState<GroupDetail | null>(null);
  const [expenses, setExpenses] = useState<Expense[]>([]);
  const [balance, setBalance] = useState<Balance | null>(null);
  const [settlements, setSettlements] = useState<Settlement[]>([]);
  const [status, setStatus] = useState("Sign in to load your groups.");
  const [busy, setBusy] = useState(false);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [authMode, setAuthMode] = useState<"login" | "register">("login");
  const [description, setDescription] = useState("");
  const [amount, setAmount] = useState("");
  const [payerId, setPayerId] = useState("");
  const [splitAmounts, setSplitAmounts] = useState<Record<string, string>>({});
  const [paymentLink, setPaymentLink] = useState("");
  const memberNames = useMemo(() => new Map((selectedGroup?.members ?? []).map((member) => [member.id, member.display_name])), [selectedGroup]);

  useEffect(() => {
    const savedToken = window.localStorage.getItem("settlemate_token");
    const savedUser = window.localStorage.getItem("settlemate_user_id");
    if (savedToken && savedUser) { setToken(savedToken); setUserId(savedUser); void loadGroups(savedToken); }
  }, []);

  async function api(path: string, options: RequestInit = {}, accessToken = token) {
    const response = await fetch(`${API_URL}${path}`, { ...options, headers: { "Content-Type": "application/json", ...(accessToken ? { Authorization: `Bearer ${accessToken}` } : {}), ...(options.headers ?? {}) } });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail ?? "Something went wrong. Please try again.");
    return data;
  }

  async function loadGroups(accessToken = token) {
    try { const data = await api("/api/v1/groups", {}, accessToken); setGroups(data); setStatus(data.length ? "Live data from your SettleMate account." : "Create a group to get started."); }
    catch (error) { window.localStorage.removeItem("settlemate_token"); window.localStorage.removeItem("settlemate_user_id"); setToken(""); setStatus(error instanceof Error ? error.message : "Your session expired. Please sign in again."); }
  }

  async function openGroup(group: Group) {
    setBusy(true); setStatus("Loading group details...");
    try {
      const [detail, groupExpenses, groupBalance, groupSettlements] = await Promise.all([api(`/api/v1/groups/${group.id}`), api(`/api/v1/groups/${group.id}/expenses`), api(`/api/v1/groups/${group.id}/balances`), api(`/api/v1/groups/${group.id}/settlements`)]);
      setSelectedGroup(detail); setExpenses(groupExpenses); setBalance(groupBalance); setSettlements(groupSettlements); setPayerId(detail.members.some((member: Member) => member.id === userId) ? userId : detail.members[0]?.id ?? ""); setSplitAmounts(Object.fromEntries(detail.members.map((member: Member) => [member.id, ""]))); setStatus("Group details loaded.");
    } catch (error) { setStatus(error instanceof Error ? error.message : "Unable to load this group."); } finally { setBusy(false); }
  }

  async function signIn(event: FormEvent) {
    event.preventDefault(); setBusy(true); setStatus("Signing in...");
    try { const path = authMode === "register" ? "/api/v1/auth/register" : "/api/v1/auth/login"; const body = authMode === "register" ? { email, display_name: displayName, password } : { email, password }; const data = await api(path, { method: "POST", body: JSON.stringify(body) }, ""); window.localStorage.setItem("settlemate_token", data.access_token); window.localStorage.setItem("settlemate_user_id", data.user_id); setToken(data.access_token); setUserId(data.user_id); await loadGroups(data.access_token); }
    catch (error) { setStatus(error instanceof Error ? error.message : "Unable to sign in."); } finally { setBusy(false); }
  }

  function logout() { window.localStorage.removeItem("settlemate_token"); window.localStorage.removeItem("settlemate_user_id"); setToken(""); setUserId(""); setGroups([]); setSelectedGroup(null); setStatus("Signed out."); }

  async function createExpense(event: FormEvent) {
    event.preventDefault(); if (!selectedGroup) return; setBusy(true); setStatus("Saving expense...");
    try { const splits = Object.fromEntries(Object.entries(splitAmounts).map(([id, value]) => [id, Number(value) * 100])); await api(`/api/v1/groups/${selectedGroup.id}/expenses`, { method: "POST", body: JSON.stringify({ description, amount_minor: Number(amount) * 100, currency: "INR", paid_by_id: payerId, splits }) }); setDescription(""); setAmount(""); await openGroup(selectedGroup); }
    catch (error) { setStatus(error instanceof Error ? error.message : "Unable to save expense."); setBusy(false); }
  }

  async function proposeSettlement() { if (!selectedGroup) return; setBusy(true); setStatus("Calculating settlement..."); try { await api(`/api/v1/groups/${selectedGroup.id}/settlements/optimize`, { method: "POST", body: "{}" }); await openGroup(selectedGroup); } catch (error) { setStatus(error instanceof Error ? error.message : "Unable to calculate settlement."); setBusy(false); } }
  async function approveSettlement(id: string) { setBusy(true); setStatus("Approving settlement..."); try { await api(`/api/v1/settlements/${id}/approve`, { method: "POST", body: "{}" }); if (selectedGroup) await openGroup(selectedGroup); } catch (error) { setStatus(error instanceof Error ? error.message : "Only a group administrator can approve this settlement."); setBusy(false); } }
  async function startPayment(transfer: Transfer) { setBusy(true); setStatus("Preparing secure Sandbox checkout..."); setPaymentLink(""); try { const result = await api(`/api/v1/transfers/${transfer.id}/payment-order`, { method: "POST", headers: { "Idempotency-Key": `demo-${transfer.id}` }, body: "{}" }); if (!result.approval_url) throw new Error("Payment was not approved by the provider; recovery is required."); setPaymentLink(result.approval_url); setStatus("Sandbox checkout is ready. Payment is not complete until PayPal confirms it."); } catch (error) { setStatus(error instanceof Error ? error.message : "Payment recovery is required."); } finally { setBusy(false); } }

  if (!token) return <main><div className="eyebrow">SettleMate · live account</div><h1>Make shared expenses feel settled.</h1><p className="lede">One clear workspace for group expenses, fair balances, and payment-ready settlements.</p><form className="card form" onSubmit={signIn}><div className="auth-tabs"><button type="button" className={authMode === "login" ? "tab active" : "tab"} onClick={() => setAuthMode("login")}>Sign in</button><button type="button" className={authMode === "register" ? "tab active" : "tab"} onClick={() => setAuthMode("register")}>Create account</button></div>{authMode === "register" && <input aria-label="Display name" placeholder="Your name" value={displayName} onChange={(event) => setDisplayName(event.target.value)} required />}<input aria-label="Email" type="email" placeholder="Email" value={email} onChange={(event) => setEmail(event.target.value)} required /><input aria-label="Password" type="password" placeholder="Password" value={password} onChange={(event) => setPassword(event.target.value)} minLength={8} required /><button type="submit" disabled={busy}>{busy ? "Working…" : authMode === "register" ? "Create account" : "Open dashboard"}</button><p className="status">{status}</p></form></main>;

  return <main><header className="topbar"><div><div className="eyebrow">SettleMate · group finance</div><h1>Keep every shared expense moving.</h1></div><button className="ghost" onClick={logout}>Sign out</button></header>{!selectedGroup ? <section className="card"><div className="section-heading"><div><span className="kicker">Dashboard</span><h2>Your groups</h2></div><button className="ghost" onClick={() => void loadGroups()}>Refresh</button></div>{groups.length ? <div className="group-grid">{groups.map((group) => <button className="group-card" key={group.id} onClick={() => void openGroup(group)}><span className="group-icon">↗</span><strong>{group.name}</strong><small>Open group workspace</small></button>)}</div> : <p>{status}</p>}</section> : <><button className="back" onClick={() => setSelectedGroup(null)}>← All groups</button><section className="card hero-card"><div><span className="kicker">Group workspace</span><h2>{selectedGroup.name}</h2><p className="status">{selectedGroup.members.length} members · {expenses.length} expenses · {settlements.length} settlements</p></div><button className="ghost" onClick={() => void openGroup({ id: selectedGroup.id, name: selectedGroup.name })}>Refresh</button></section><div className="dashboard-grid"><section className="card"><div className="section-heading"><div><span className="kicker">People</span><h3>Members</h3></div></div><ul className="clean-list">{selectedGroup.members.map((member) => <li key={member.id}><span className="avatar">{member.display_name.slice(0, 1).toUpperCase()}</span><span>{member.display_name}{member.id === userId ? " · you" : ""}</span></li>)}</ul></section><section className="card"><div className="section-heading"><div><span className="kicker">Live calculation</span><h3>Balances</h3></div><span className="pill">{balance?.currency ?? "—"}</span></div>{balance && Object.entries(balance.balances).map(([id, value]) => <div className="balance-row" key={id}><span>{memberNames.get(id) ?? "Member"}</span><strong className={value < 0 ? "negative" : "positive"}>{value >= 0 ? "+" : "−"}{Math.abs(value / 100).toFixed(2)} {balance.currency}</strong></div>)}</section></div><div className="dashboard-grid"><section className="card"><span className="kicker">Ledger</span><h3>Expenses</h3>{expenses.length ? <div className="expense-list">{expenses.map((expense) => <div className="expense-row" key={expense.id}><div><strong>{expense.description}</strong><small>Paid by {memberNames.get(expense.paid_by_id) ?? "member"}</small></div><strong>{(expense.amount_minor / 100).toFixed(2)} {expense.currency}</strong></div>)}</div> : <p>No expenses yet.</p>}<form className="expense-form" onSubmit={createExpense}><h4>Add an expense</h4><input aria-label="Expense description" placeholder="Dinner, taxi, supplies…" value={description} onChange={(event) => setDescription(event.target.value)} required /><input aria-label="Expense amount" type="number" min="0.01" step="0.01" placeholder="Amount in INR" value={amount} onChange={(event) => setAmount(event.target.value)} required /><select aria-label="Payer" value={payerId} onChange={(event) => setPayerId(event.target.value)}>{selectedGroup.members.map((member) => <option key={member.id} value={member.id}>{member.display_name} paid</option>)}</select>{selectedGroup.members.map((member) => <input key={member.id} aria-label={`Split for ${member.display_name}`} type="number" min="0.01" step="0.01" placeholder={`${member.display_name} split`} value={splitAmounts[member.id] ?? ""} onChange={(event) => setSplitAmounts({ ...splitAmounts, [member.id]: event.target.value })} required />)}<button type="submit" disabled={busy}>Add expense</button></form></section><section className="card"><span className="kicker">Settlement plan</span><h3>Suggested transfers</h3><button onClick={() => void proposeSettlement()} disabled={busy}>Calculate settlement</button>{settlements.length ? settlements.map((settlement) => <div className="settlement" key={settlement.id}><div className="section-heading"><strong>{settlement.status}</strong>{settlement.status === "proposed" && <button className="small" onClick={() => void approveSettlement(settlement.id)}>Approve</button>}</div>{settlement.transfers.map((transfer) => <div className="transfer" key={transfer.id}><div><strong>{memberNames.get(transfer.from_user_id) ?? "Member"} → {memberNames.get(transfer.to_user_id) ?? "Member"}</strong><small>{transfer.status}</small></div><strong>{(transfer.amount_minor / 100).toFixed(2)} {balance?.currency}</strong>{settlement.status === "approved" && transfer.status === "pending" && transfer.from_user_id === userId && <button className="small" onClick={() => void startPayment(transfer)}>Open Sandbox payment</button>}</div>)}</div>) : <p className="status">No settlement proposed yet.</p>}{paymentLink && <a className="approval-link" href={paymentLink} target="_blank" rel="noreferrer">Continue to PayPal Sandbox ↗</a>}</section></div><p className="status footer-status">{status}</p></>}</main>;
}
