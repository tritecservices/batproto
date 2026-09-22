import { useTitle } from "../router";
import { SITE } from "../config";

// TEMPLATE. Have this reviewed before launch; it is a starting point, not legal advice.
export function Legal() {
  useTitle(`Privacy & legal — ${SITE.name}`);
  return (
    <section className="section page-top">
      <div className="wrap narrow prose">
        <p className="eyebrow">Privacy &amp; legal</p>
        <h1>Privacy notice</h1>
        <p>{SITE.company} ("we") is the controller for personal data submitted through this website.</p>
        <h2>What we collect</h2>
        <p>Only what you type into the contact form: name, email, organisation and message. We don't use tracking cookies or third-party analytics.</p>
        <h2>Why, and on what basis</h2>
        <p>To reply to your enquiry, with your consent, and where relevant to take steps toward a contract you've asked about. You can withdraw consent at any time by emailing us.</p>
        <h2>Where it's stored</h2>
        <p>Form submissions are processed by our website host, Netlify, which may store data outside the UK under appropriate safeguards. We keep enquiries for up to 24 months, then delete them.</p>
        <h2>Your rights</h2>
        <p>Under UK GDPR you can ask to see, correct or delete your data, or object to how we use it. Email <a href={`mailto:${SITE.email}`}>{SITE.email}</a>. You can also complain to the Information Commissioner's Office (ico.org.uk).</p>
        <h2>The demo search</h2>
        <p>Search terms typed into the demo are sent to our search service to return results and aren't linked to you. Results come from public community forums and remain the work of their authors under their original licences.</p>
        <h2>Trademarks</h2>
        <p>Product names mentioned on this site belong to their owners and are used only to describe compatibility. We are not affiliated with or endorsed by them.</p>
      </div>
    </section>
  );
}
