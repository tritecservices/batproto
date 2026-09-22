import { useState, type FormEvent } from "react";
import { Link, useTitle } from "../router";
import { SITE } from "../config";
import { PRODUCTS } from "../data/products";

// Submits to Netlify Forms. The matching hidden form in index.html is what makes
// Netlify register "contact" at deploy time; keep field names in sync with it.
function encode(data: FormData): string {
  return new URLSearchParams(data as unknown as Record<string, string>).toString();
}

export function Contact() {
  useTitle(`Contact — ${SITE.name}`);
  const initial = new URLSearchParams(window.location.search).get("interest") ?? "";
  const [status, setStatus] = useState<"idle" | "sending" | "sent" | "error">("idle");

  async function onSubmit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const data = new FormData(e.currentTarget);
    data.set("form-name", "contact");
    setStatus("sending");
    try {
      const res = await fetch("/", {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: encode(data),
      });
      if (!res.ok) throw new Error(String(res.status));
      setStatus("sent");
    } catch {
      setStatus("error");
    }
  }

  if (status === "sent") {
    return (
      <section className="section page-top">
        <div className="wrap narrow">
          <h1>Thanks, we've got it</h1>
          <p className="lede">We'll reply within two working days.</p>
          <Link to="/products" className="btn btn-ghost">Back to products</Link>
        </div>
      </section>
    );
  }

  return (
    <section className="section page-top">
      <div className="wrap narrow">
        <p className="eyebrow">Contact</p>
        <h1>Tell us what slows your team down</h1>
        <p className="lede">Register interest in a tool, ask about support, or suggest something we should build.</p>

        <form className="form" name="contact" method="POST" onSubmit={onSubmit}>
          <input type="hidden" name="form-name" value="contact" />
          <p className="hp"><label>Leave this empty <input name="bot-field" tabIndex={-1} autoComplete="off" /></label></p>

          <div className="field-row">
            <label>Name<input name="name" required autoComplete="name" /></label>
            <label>Email<input name="email" type="email" required autoComplete="email" /></label>
          </div>
          <label>Organisation<input name="organisation" autoComplete="organization" /></label>
          <label>Interested in
            <select name="interest" defaultValue={initial}>
              <option value="">Not sure yet</option>
              {PRODUCTS.map((p) => <option key={p.slug} value={p.slug}>{p.name}</option>)}
              <option value="idea">An idea for a new tool</option>
            </select>
          </label>
          <label>Message<textarea name="message" rows={5} required /></label>
          <p className="warn-note">
            Please don't include survey locations, grid references or client data in this form.
          </p>
          <label className="check">
            <input type="checkbox" name="consent" value="yes" required />
            <span>I'm happy for {SITE.name} to store these details to reply to me. See the <Link to="/legal">privacy notice</Link>.</span>
          </label>
          <button className="btn btn-primary" disabled={status === "sending"}>
            {status === "sending" ? "Sending…" : "Send"}
          </button>
          {status === "error" && (
            <p className="notice error" role="alert">
              That didn't send. Please try again, or email <a href={`mailto:${SITE.email}`}>{SITE.email}</a>.
            </p>
          )}
        </form>
      </div>
    </section>
  );
}
