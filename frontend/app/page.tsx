"use client";

import { FormEvent, useEffect, useState } from "react";

type Group = { id: string; name: string };

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export default function Home() {
  const [groups, setGroups] = useState<Group[]>([]);
  const [token, setToken] = useState("");
  const [status, setStatus] = useState("Sign in to load your groups.");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");

  useEffect(() => {
    const saved = window.localStorage.getItem("settlemate_token");
    if (saved) {
      setToken(saved);
      void loadGroups(saved);
    }
  }, []);

  async function loadGroups(accessToken: string) {
    const response = await fetch(`${API_URL}/api/v1/groups`, {
      headers: { Authorization: `Bearer ${accessToken}` },
    });
    if (!response.ok) {
      setStatus("Your session expired. Please sign in again.");
      return;
    }
    setGroups(await response.json());
    setStatus("Live data from your SettleMate account.");
  }

  async function signIn(event: FormEvent) {
    event.preventDefault();
    setStatus("Signing in...");
    const response = await fetch(`${API_URL}/api/v1/auth/login`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, password }),
    });
    if (!response.ok) {
      setStatus("Unable to sign in with those credentials.");
      return;
    }
    const data = await response.json();
    window.localStorage.setItem("settlemate_token", data.access_token);
    setToken(data.access_token);
    await loadGroups(data.access_token);
  }

  return (
    <main>
      <div className="eyebrow">SettleMate · live account</div>
      <h1>AI-powered group settlement orchestration.</h1>
      {!token ? (
        <form className="card form" onSubmit={signIn}>
          <strong>Sign in</strong>
          <input aria-label="Email" type="email" placeholder="Email" value={email} onChange={(event) => setEmail(event.target.value)} required />
          <input aria-label="Password" type="password" placeholder="Password" value={password} onChange={(event) => setPassword(event.target.value)} required />
          <button type="submit">Load my groups</button>
          <p>{status}</p>
        </form>
      ) : (
        <section className="card">
          <strong>Your groups</strong>
          {groups.length === 0 ? <p>{status}</p> : <ul>{groups.map((group) => <li key={group.id}>{group.name}</li>)}</ul>}
        </section>
      )}
    </main>
  );
}
