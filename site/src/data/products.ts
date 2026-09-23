// The product catalogue. Edit here; every page reads from this list.
//
// Keep claims honest: `status` drives the badge, and anything not yet shipped says
// so. Each product is an original build against published formats and APIs. See
// agents/product-scout.yaml for the rules on what may and may not be productised.

export type Status = "available" | "beta" | "in-development" | "planned";

export interface Product {
  slug: string;
  name: string;
  status: Status;
  summary: string; // one line for cards
  problem: string; // the pain, in the customer's words
  features: string[];
  worksWith: string[];
  delivery: string; // how it ships
  notFor: string; // what it deliberately does not do
  icon: "path" | "wave" | "shield" | "moon" | "chat";
}

export const STATUS_LABEL: Record<Status, string> = {
  available: "Available",
  beta: "Beta",
  "in-development": "In development",
  planned: "Planned",
};

export const PRODUCTS: Product[] = [
  {
    slug: "project-path-doctor",
    name: "Project Path Doctor",
    status: "in-development",
    icon: "path",
    summary: "Finds and repairs broken layer links across every QGIS project on a share.",
    problem:
      "The file server was renamed, or a drive letter changed, and now dozens of survey projects open full of red exclamation marks. Someone fixes them one layer at a time.",
    features: [
      "Scans whole folders of .qgs and .qgz projects without opening QGIS",
      "Reports every layer whose data source no longer resolves",
      "Bulk-rewrites old paths to new ones, with a dry run first",
      "Writes a backup of every project before changing it",
      "CSV report you can attach to the change ticket",
    ],
    worksWith: ["QGIS 3.x", "Windows file shares", "SMB / NAS storage"],
    delivery: "Windows command-line tool, with an optional QGIS plugin front end.",
    notFor: "Moving or copying the data itself. It fixes the links, not the files.",
  },
  {
    slug: "guano-lens",
    name: "GUANO Lens",
    status: "in-development",
    icon: "wave",
    summary: "Audits the metadata inside bat recordings before it reaches the analysis stage.",
    problem:
      "A detector was deployed with the wrong clock, or no GPS fix, and nobody notices until the report is being written.",
    features: [
      "Reads GUANO metadata from WAV files without loading the audio",
      "Flags missing coordinates, clock drift and mismatched detector serials",
      "Summarises each deployment: nights, file counts, first and last recording",
      "Exports detector locations as CSV or GeoPackage for QGIS and ArcGIS Pro",
    ],
    worksWith: ["GUANO-tagged WAV files", "QGIS", "ArcGIS Pro"],
    delivery: "Standalone Windows and macOS app; also a Python command-line tool.",
    notFor: "Species identification. Classification stays with your chosen classifier and your ecologists.",
  },
  {
    slug: "night-summary",
    name: "Night Summary",
    status: "planned",
    icon: "moon",
    summary: "Turns classifier exports into per-night, per-species tables ready for a report.",
    problem:
      "Every survey season, someone rebuilds the same pivot tables from classifier CSVs by hand, and a copy-paste slip ends up in a client report.",
    features: [
      "Reads classifier CSV exports and groups passes by night, site and species",
      "Handles nights that cross midnight correctly",
      "Keeps auto-ID and manual-ID separate so QA is visible",
      "Outputs tables and charts in a consistent house style",
    ],
    worksWith: ["Classifier CSV exports", "Excel", "Word"],
    delivery: "Desktop tool with a reusable per-client template.",
    notFor: "Replacing manual verification. It shows what was verified; it does not verify.",
  },
  {
    slug: "sensitive-site-screen",
    name: "Sensitive Site Screen",
    status: "planned",
    icon: "shield",
    summary: "Checks reports for protected-species locations before they leave the building.",
    problem:
      "A report bound for a public planning portal still contains a precise grid reference for a roost or sett.",
    features: [
      "Scans Word and PDF documents for grid references and protected-species terms",
      "Highlights each hit in context so an ecologist can decide",
      "Suggests a coarser grid reference where one is appropriate",
      "Produces a signed-off checklist for the project file",
    ],
    worksWith: ["Microsoft Word", "PDF"],
    delivery: "Desktop tool; Word add-in under consideration.",
    notFor: "Making the publication decision. It is a second pair of eyes, not a sign-off.",
  },
  {
    slug: "managed-gis-support",
    name: "Managed GIS Support",
    status: "available",
    icon: "chat",
    summary: "IT and GIS support from people who know what a transect is.",
    problem:
      "General IT providers can reset a password but not explain why a GeoPackage locks on a network share in the middle of a field season.",
    features: [
      "Support for QGIS, ArcGIS Pro, Google Earth Pro and bat acoustic software",
      "Answers grounded in our own searchable troubleshooting history",
      "Laptop builds that work in a field with no signal",
      "Sensible handling of protected-species data",
    ],
    worksWith: ["QGIS", "ArcGIS Pro", "Google Earth Pro", "Microsoft 365"],
    delivery: "Monthly managed service.",
    notFor: "Ecological consultancy. We look after the tools; your ecologists do the ecology.",
  },
];
