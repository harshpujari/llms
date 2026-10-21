// --- Minimal markdown renderer ------------------------------------------
// The model emits markdown, and nothing external is loadable here (the page is
// served offline by nginx), so this covers the subset an LLM actually produces:
// headings, bold/italic, inline + fenced code, lists, links.

function escapeHtml(s) {
  return s.replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

function inlineMd(s) {
  return s
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>")
    // Only http(s) becomes a link -- never javascript:, which would execute
    // whatever the model happened to generate.
    .replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (m, text, href) =>
      /^https?:\/\//i.test(href) ? `<a href="${href}" target="_blank" rel="noopener">${text}</a>` : m);
}

// --- GFM pipe tables -----------------------------------------------------
// MarkItDown turns every PDF table and spreadsheet into one of these, so a
// renderer without them shows walls of literal pipes.

function splitRow(line) {
  let t = line.trim();
  if (t.startsWith("|")) t = t.slice(1);
  if (t.endsWith("|")) t = t.slice(0, -1);
  return t.split("|");
}

function isTableRow(line) {
  return line.trim().startsWith("|") && line.includes("|", 1);
}

// The row of dashes under the header: cells of only -, : and space.
function isDelimiterRow(line) {
  if (!line.includes("|") || !line.includes("-")) return false;
  const cells = splitRow(line);
  return cells.length > 0 && cells.every(c => /^\s*:?-+:?\s*$/.test(c));
}

function alignOf(cell) {
  const t = cell.trim();
  if (t.startsWith(":") && t.endsWith(":")) return "center";
  if (t.endsWith(":")) return "right";
  return "";
}

function renderTable(header, aligns, rows) {
  const cell = (tag, text, align) =>
    `<${tag}${align ? ` style="text-align:${align}"` : ""}>${inlineMd(text.trim())}</${tag}>`;

  let out = '<div class="table-wrap"><table><thead><tr>';
  header.forEach((c, i) => { out += cell("th", c, aligns[i]); });
  out += "</tr></thead><tbody>";

  for (const row of rows) {
    out += "<tr>";
    // Pad and truncate to the header width, the way GFM specifies -- extracted
    // tables are ragged, and a stray cell would otherwise skew the whole grid.
    for (let i = 0; i < header.length; i++) out += cell("td", row[i] ?? "", aligns[i]);
    out += "</tr>";
  }
  return out + "</tbody></table></div>";
}

export function renderMarkdown(src) {
  // Escaped up front, so everything below composes tags around inert text.
  const lines = escapeHtml(src).split("\n");
  let html = "", inCode = false, code = "";

  // Open lists, outermost first, so indented items nest instead of closing the
  // parent -- otherwise a sub-bullet ends the <ol> and the next item restarts at 1.
  const stack = [];
  const closeOne = () => {
    const top = stack.pop();
    if (top.liOpen) html += "</li>";
    html += `</${top.tag}>`;
  };
  const closeTo = indent => {
    while (stack.length && stack[stack.length - 1].indent > indent) closeOne();
  };
  const closeAll = () => { while (stack.length) closeOne(); };

  // Indexed, not for..of: tables need to look at the next line to know they're
  // a table at all, and then consume as many rows as follow.
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (line.trim().startsWith("```")) {
      if (inCode) { html += `<pre><code>${code}</code></pre>`; code = ""; inCode = false; }
      else { closeAll(); inCode = true; }
      continue;
    }
    if (inCode) { code += line + "\n"; continue; }
    if (!line.trim()) continue; // blank lines don't end a list; indentation does

    const heading = line.match(/^(#{1,4})\s+(.*)$/);
    if (heading) {
      closeAll();
      const level = heading[1].length;

      // "## Page 3" / "## Slide 7" are the boundary markers extraction writes.
      // Shown as a labelled rule rather than another heading, so they read as
      // boundaries instead of competing with the document's real headings.
      const boundary = level === 2 && heading[2].match(/^(Page|Slide) (\d+)$/);
      if (boundary) {
        html += `<div class="page-sep"><span>${boundary[1]} ${boundary[2]}</span></div>`;
        continue;
      }

      html += `<h${level}>${inlineMd(heading[2])}</h${level}>`;
      continue;
    }

    // A table is a row whose next line is the dashes. Checked before lists,
    // since a delimiter row on its own would otherwise look like a bullet.
    if (isTableRow(line) && i + 1 < lines.length && isDelimiterRow(lines[i + 1])) {
      closeAll();
      const header = splitRow(line);
      const aligns = splitRow(lines[i + 1]).map(alignOf);

      const rows = [];
      let j = i + 2;
      while (j < lines.length && isTableRow(lines[j]) && !isDelimiterRow(lines[j])) {
        rows.push(splitRow(lines[j]));
        j++;
      }
      html += renderTable(header, aligns, rows);
      i = j - 1; // the loop's ++ moves past the last row consumed
      continue;
    }

    // A bare rule -- no pipes, so it can't be a table delimiter.
    if (/^\s*([-*_])\s*(\1\s*){2,}$/.test(line)) {
      closeAll();
      html += "<hr>";
      continue;
    }

    // llama3.2 emits "• " far more often than "- " or "* ", so accept both.
    const ul = line.match(/^([ \t]*)[-*+•·‣]\s+(.*)$/);
    const ol = line.match(/^([ \t]*)\d+[.)]\s+(.*)$/);
    if (ul || ol) {
      const [, lead, content] = ul || ol;
      const tag = ul ? "ul" : "ol";
      const indent = lead.replace(/\t/g, "    ").length;

      closeTo(indent);
      let top = stack[stack.length - 1];
      if (!top || top.indent < indent) {
        // Nested list opens inside the parent's still-open <li>.
        html += `<${tag}>`;
        stack.push({ tag, indent, liOpen: false });
      } else if (top.tag !== tag) {
        closeOne();
        html += `<${tag}>`;
        stack.push({ tag, indent, liOpen: false });
      }
      top = stack[stack.length - 1];
      if (top.liOpen) html += "</li>";
      html += `<li>${inlineMd(content)}`;
      top.liOpen = true;
      continue;
    }

    closeAll();
    html += `<p>${inlineMd(line)}</p>`;
  }

  // Mid-stream the closing fence hasn't arrived yet; render what's there.
  if (inCode) html += `<pre><code>${code}</code></pre>`;
  closeAll();
  return html;
}
