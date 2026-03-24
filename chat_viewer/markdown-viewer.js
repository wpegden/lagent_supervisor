function escapeHtml(value) {
  return value
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function escapeAttr(value) {
  return escapeHtml(value);
}

function renderInline(text) {
  let html = escapeHtml(text);
  html = html.replace(/\[([^\]]+)\]\(([^)]+)\)/g, (_match, label, href) => {
    return `<a href="${escapeAttr(href)}" target="_blank" rel="noopener noreferrer">${label}</a>`;
  });
  html = html.replace(/`([^`]+)`/g, "<code>$1</code>");
  html = html.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  html = html.replace(/\*([^*]+)\*/g, "<em>$1</em>");
  return html;
}

function renderMarkdown(markdown) {
  const lines = markdown.replace(/\r\n?/g, "\n").split("\n");
  const html = [];
  let paragraph = [];
  let listType = null;
  let listItems = [];
  let inCodeBlock = false;
  let codeFence = "";
  let codeLines = [];

  function flushParagraph() {
    if (!paragraph.length) {
      return;
    }
    html.push(`<p>${renderInline(paragraph.join(" "))}</p>`);
    paragraph = [];
  }

  function flushList() {
    if (!listType || !listItems.length) {
      listType = null;
      listItems = [];
      return;
    }
    const tag = listType === "ol" ? "ol" : "ul";
    html.push(`<${tag}>${listItems.map((item) => `<li>${renderInline(item)}</li>`).join("")}</${tag}>`);
    listType = null;
    listItems = [];
  }

  function flushCodeBlock() {
    if (!inCodeBlock) {
      return;
    }
    html.push(`<pre><code>${escapeHtml(codeLines.join("\n"))}</code></pre>`);
    inCodeBlock = false;
    codeFence = "";
    codeLines = [];
  }

  lines.forEach((line) => {
    const fenced = line.match(/^(```|~~~)(.*)$/);
    if (fenced) {
      flushParagraph();
      flushList();
      if (inCodeBlock && fenced[1] === codeFence) {
        flushCodeBlock();
      } else if (!inCodeBlock) {
        inCodeBlock = true;
        codeFence = fenced[1];
        codeLines = [];
      } else {
        codeLines.push(line);
      }
      return;
    }

    if (inCodeBlock) {
      codeLines.push(line);
      return;
    }

    const trimmed = line.trim();
    if (!trimmed) {
      flushParagraph();
      flushList();
      return;
    }

    const heading = trimmed.match(/^(#{1,6})\s+(.*)$/);
    if (heading) {
      flushParagraph();
      flushList();
      const level = heading[1].length;
      html.push(`<h${level}>${renderInline(heading[2])}</h${level}>`);
      return;
    }

    if (/^---+$/.test(trimmed) || /^\*\*\*+$/.test(trimmed)) {
      flushParagraph();
      flushList();
      html.push("<hr>");
      return;
    }

    const blockquote = trimmed.match(/^>\s?(.*)$/);
    if (blockquote) {
      flushParagraph();
      flushList();
      html.push(`<blockquote><p>${renderInline(blockquote[1])}</p></blockquote>`);
      return;
    }

    const ordered = trimmed.match(/^\d+\.\s+(.*)$/);
    if (ordered) {
      flushParagraph();
      if (listType && listType !== "ol") {
        flushList();
      }
      listType = "ol";
      listItems.push(ordered[1]);
      return;
    }

    const unordered = trimmed.match(/^[-*+]\s+(.*)$/);
    if (unordered) {
      flushParagraph();
      if (listType && listType !== "ul") {
        flushList();
      }
      listType = "ul";
      listItems.push(unordered[1]);
      return;
    }

    if (listType) {
      flushList();
    }
    paragraph.push(trimmed);
  });

  flushParagraph();
  flushList();
  flushCodeBlock();

  return html.join("\n");
}

async function main() {
  const params = new URLSearchParams(window.location.search);
  const repoName = params.get("repo") || "";
  const path = params.get("path") || "";
  const label = params.get("label") || "Markdown file";
  const source = params.get("source") || label;

  const title = document.querySelector("#doc-title");
  const meta = document.querySelector("#doc-meta");
  const content = document.querySelector("#markdown-content");
  const backLink = document.querySelector("#back-link");

  title.textContent = label;
  meta.textContent = source;
  backLink.href = repoName ? `../#${repoName}` : "../";

  if (!path) {
    meta.textContent = "Missing markdown file path.";
    content.innerHTML = "<p>Could not locate the requested markdown file.</p>";
    return;
  }

  try {
    const response = await fetch(`../${path}`, { cache: "no-store" });
    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }
    const markdown = await response.text();
    content.innerHTML = renderMarkdown(markdown);
  } catch (error) {
    meta.textContent = `${source} · failed to load`;
    content.innerHTML = `<p>Could not load this markdown file.</p><pre><code>${escapeHtml(String(error))}</code></pre>`;
  }
}

main();
