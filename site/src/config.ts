// One place to rebrand. Everything visible about the company comes from here.
export const SITE = {
  name: "Ecomsp Labs",
  tagline: "Software for the people who survey the countryside.",
  email: "hello@example.com", // EDIT
  company: "Ecomsp Labs", // EDIT: registered company name + number for the footer
  location: "United Kingdom",
};

// Brain API base URL, set per environment in Netlify (Site settings > Environment
// variables). Blank means the demo runs on bundled sample results.
export const BRAIN_API_URL: string = (import.meta.env.VITE_BRAIN_API_URL ?? "").replace(/\/$/, "");
