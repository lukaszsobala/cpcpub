package je.qd.cpcpub;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;

/**
 * Uploads a result to a hub, the way `cpcpub --submit` does -- submit_document()
 * in bench/src/bench.c, which this follows step for step.
 *
 * The app does this itself rather than passing --submit to the benchmark
 * because the benchmark has no TLS of its own: it hands an https:// URL to
 * curl, and Android has no curl. Java has TLS, and the request is the same.
 */
final class Hub {
    private Hub() {}

    /** What the hub keeps of a label and of the notes, in bytes. */
    static final int MAX_LABEL = 200;
    static final int MAX_NOTES = 2000;

    /**
     * The documents in the benchmark's stdout, each as the exact text it
     * printed: one object, or a --variants array of them. Cut out of the text
     * rather than parsed and written again, so what is uploaded is what was
     * measured, byte for byte, as when the benchmark uploads it.
     */
    static List<String> documents(String raw) {
        List<String> docs = new ArrayList<>();
        String text = raw.trim();
        if (!text.startsWith("[")) {
            if (!text.isEmpty()) docs.add(text);
            return docs;
        }
        int depth = 0;
        int start = -1;
        boolean inString = false;
        for (int i = 1; i < text.length(); i++) {
            char c = text.charAt(i);
            if (inString) {
                if (c == '\\') i++;
                else if (c == '"') inString = false;
                continue;
            }
            if (c == '"') {
                inString = true;
            } else if (c == '{' || c == '[') {
                if (depth++ == 0) start = i;
            } else if (c == '}' || c == ']') {
                if (depth > 0 && --depth == 0) docs.add(text.substring(start, i + 1));
            }
        }
        return docs;
    }

    /** At most maxBytes of UTF-8, never cutting a character in half. */
    static String trimUtf8(String s, int maxBytes) {
        byte[] bytes = s.getBytes(StandardCharsets.UTF_8);
        if (bytes.length <= maxBytes) return s;
        int n = maxBytes;
        while (n > 0 && (bytes[n] & 0xc0) == 0x80) n--;   // a continuation byte
        return new String(bytes, 0, n, StandardCharsets.UTF_8);
    }

    /** Percent-encoding of everything but the unreserved set, as url_encode(). */
    static String encode(String s) {
        StringBuilder out = new StringBuilder();
        for (byte b : s.getBytes(StandardCharsets.UTF_8)) {
            int c = b & 0xff;
            if ((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9')
                    || c == '-' || c == '.' || c == '_' || c == '~') {
                out.append((char) c);
            } else {
                out.append('%').append(Character.toUpperCase(Character.forDigit(c >> 4, 16)))
                    .append(Character.toUpperCase(Character.forDigit(c & 15, 16)));
            }
        }
        return out.toString();
    }

    /**
     * A reply from the network, made safe to show: control characters as
     * \xNN and the whole capped, as print_reply() does. The delete token an
     * anonymous upload gets back is in here, so it must survive intact.
     */
    static String printable(String body) {
        String s = body.replaceAll("[ \\t\\r\\n]+$", "");
        StringBuilder out = new StringBuilder();
        int max = 1024;
        for (int i = 0; i < s.length() && i < max; i++) {
            char c = s.charAt(i);
            if (c >= 0x20 && c != 0x7f) out.append(c);
            else out.append(String.format("\\x%02x", (int) c));
        }
        if (s.length() > max) out.append(" ...");
        return out.toString();
    }

    /**
     * POST one document. `variant` names the build it came from on a
     * --variants run, appended to the label, which is what tells the rows on
     * the board apart. Returns what to log; `ok[0]` says whether it landed.
     */
    static String submit(String hub, String token, String label, String notes,
                         String variant, String doc, boolean[] ok) {
        StringBuilder log = new StringBuilder();
        ok[0] = false;
        String tag = variant == null || variant.isEmpty() ? "" : "(" + variant + ")";
        int reserve = tag.isEmpty() ? 0 : tag.getBytes(StandardCharsets.UTF_8).length + 1;
        String lab = trimUtf8(label, MAX_LABEL - reserve);
        String note = trimUtf8(notes, MAX_NOTES);
        if (!lab.equals(label)) {
            log.append("submit: label trimmed to ").append(MAX_LABEL)
                .append(" bytes, which is what the hub keeps\n");
        }
        if (!note.equals(notes)) {
            log.append("submit: notes trimmed to ").append(MAX_NOTES)
                .append(" bytes, which is what the hub keeps\n");
        }
        if (!tag.isEmpty()) lab = lab.isEmpty() ? tag : lab + " " + tag;

        String base = hub.replaceAll("/+$", "");
        StringBuilder url = new StringBuilder(base).append("/api/runs");
        if (!lab.isEmpty()) url.append("?label=").append(encode(lab));
        if (!note.isEmpty()) url.append(lab.isEmpty() ? '?' : '&').append("notes=").append(encode(note));

        byte[] body = doc.getBytes(StandardCharsets.UTF_8);
        log.append("submitting ").append(body.length).append(" bytes to ").append(base).append(" ...\n");
        HttpURLConnection conn = null;
        try {
            conn = (HttpURLConnection) new URL(url.toString()).openConnection();
            // Not followed: an upload carrying a token goes where it was
            // addressed, and nowhere a reply points it.
            conn.setInstanceFollowRedirects(false);
            conn.setConnectTimeout(15000);
            conn.setReadTimeout(60000);
            conn.setRequestMethod("POST");
            conn.setDoOutput(true);
            conn.setFixedLengthStreamingMode(body.length);
            conn.setRequestProperty("Content-Type", "application/json");
            if (token != null && !token.isEmpty()) {
                conn.setRequestProperty("Authorization", "Bearer " + token);
            }
            try (OutputStream out = conn.getOutputStream()) {
                out.write(body);
            }
            int status = conn.getResponseCode();
            InputStream in = status < 400 ? conn.getInputStream() : conn.getErrorStream();
            String reply = in == null ? "" : readAll(in);
            if (status >= 200 && status < 300) {
                log.append("uploaded: ").append(printable(reply)).append('\n');
                ok[0] = true;
            } else if (status >= 300 && status < 400 && conn.getHeaderField("Location") != null) {
                log.append("the hub redirected the upload (HTTP ").append(status).append(") to ")
                    .append(printable(conn.getHeaderField("Location")))
                    .append("\nsubmit to that address instead; this app does not follow redirects\n");
            } else {
                log.append("hub refused the upload (HTTP ").append(status).append("): ")
                    .append(printable(reply)).append('\n');
            }
        } catch (IOException | IllegalArgumentException e) {
            log.append("submit failed: ").append(e.getMessage()).append('\n');
        } finally {
            if (conn != null) conn.disconnect();
        }
        return log.toString();
    }

    private static String readAll(InputStream in) throws IOException {
        try (InputStream src = in) {
            ByteArrayOutputStream buf = new ByteArrayOutputStream();
            byte[] chunk = new byte[8192];
            for (int n; (n = src.read(chunk)) > 0; ) buf.write(chunk, 0, n);
            return buf.toString("UTF-8");
        }
    }
}
