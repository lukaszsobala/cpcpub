package je.qd.cpcpub;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.Intent;
import android.content.SharedPreferences;
import android.graphics.Insets;
import android.graphics.Typeface;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.text.InputType;
import android.text.TextUtils;
import android.util.TypedValue;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.view.WindowInsets;
import android.view.WindowManager;
import android.widget.Button;
import android.widget.CheckBox;
import android.widget.EditText;
import android.widget.HorizontalScrollView;
import android.widget.LinearLayout;
import android.widget.ProgressBar;
import android.widget.RadioButton;
import android.widget.RadioGroup;
import android.widget.ScrollView;
import android.widget.TableLayout;
import android.widget.TableRow;
import android.widget.TextView;

import java.io.BufferedReader;
import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.nio.charset.StandardCharsets;
import java.text.SimpleDateFormat;
import java.util.ArrayList;
import java.util.Date;
import java.util.List;
import java.util.Locale;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;
import org.json.JSONTokener;

/**
 * The one screen: what to run, a Run button, and the results as a table. Like
 * the desktop window it measures nothing itself -- it builds the benchmark's
 * command line, runs the release binary, and reads back what it prints.
 */
public final class MainActivity extends Activity {
    // One pass of the suite: nine warm-ups and fifteen measured phases, plus a
    // tenth of a second of setup. The desktop window's estimate, unchanged.
    private static final int WARMUPS_PER_PASS = 9;
    private static final int PHASES_PER_PASS = 15;
    private static final double SETUP_SECONDS_PER_PASS = 0.1;
    private static final int SAVE_REQUEST = 1;

    private final Handler main = new Handler(Looper.getMainLooper());
    private SharedPreferences prefs;

    private RadioGroup modes;
    private RadioGroup variants;
    private CheckBox upload;
    private LinearLayout uploadFields;
    private LinearLayout advancedFields;
    private EditText hub;
    private EditText token;
    private EditText label;
    private EditText notes;
    private EditText threads;
    private EditText cpus;
    private EditText seconds;
    private EditText reps;
    private EditText warmup;
    private Button runButton;
    private Button stopButton;
    private ProgressBar progress;
    private TextView estimate;
    private TextView command;
    private TextView status;
    private LinearLayout results;
    private Button logButton;
    private Button saveButton;
    private Button shareButton;
    private TextView log;

    private Process proc;
    private long started;
    private double estimated;
    private boolean leftDuringRun;
    private boolean uploading;
    private String lastRaw;
    private String lastName;

    // -- the screen ------------------------------------------------------

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        prefs = getSharedPreferences("form", MODE_PRIVATE);

        ScrollView scroll = new ScrollView(this);
        LinearLayout page = new LinearLayout(this);
        page.setOrientation(LinearLayout.VERTICAL);
        int pad = dp(16);
        page.setPadding(pad, dp(8), pad, pad);
        scroll.addView(page);
        setContentView(scroll);
        // Android 15 draws an app under the status and navigation bars unless
        // the app moves out of their way; earlier versions report no insets.
        if (Build.VERSION.SDK_INT >= 30) {
            scroll.setOnApplyWindowInsetsListener((v, insets) -> {
                Insets bars = insets.getInsets(
                    WindowInsets.Type.systemBars() | WindowInsets.Type.ime());
                v.setPadding(bars.left, bars.top, bars.right, bars.bottom);
                return WindowInsets.CONSUMED;
            });
        }

        page.addView(heading("Run"));
        modes = radios(page, new String[] {
            "Both: all threads at once, then each core on its own",
            "Multi-threaded only", "Per-core only"}, prefs.getInt("mode", 0));
        page.addView(note("Both is the one to upload: neither half means much "
            + "without the other."));

        page.addView(heading("Variants"));
        // Two choices, not the desktop's three: which variants differ is fixed by
        // the -march a binary was built for, not by the processor it runs on, and
        // the Android build's armv8-a has both a vector unit and FMA, so all four
        // of its variants are distinct and --variants would run the same four as
        // --variants=all.
        variants = radios(page, new String[] {
            "Baseline only (scalar-nofma)", "All four, and compare them"},
            prefs.getInt("variants", 0));
        page.addView(note("The benchmark carries four compilations of its kernels: "
            + "auto-vectorisation off and on, crossed with fused multiply-add off and "
            + "on. The baseline is the one results compare across processors; the "
            + "other three show how much this processor gains from each."));

        upload = new CheckBox(this);
        upload.setText("Upload the result to a hub");
        upload.setChecked(prefs.getBoolean("upload", false));
        page.addView(upload, spaced());
        uploadFields = column();
        hub = field(uploadFields, "Hub URL", getString(R.string.hub_url),
            InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_URI, "hub");
        token = field(uploadFields, "Token (from the hub's Account tab; empty uploads "
            + "anonymously)", "", InputType.TYPE_CLASS_TEXT
            | InputType.TYPE_TEXT_VARIATION_PASSWORD, "token");
        label = field(uploadFields, "Label", "short name for this phone",
            InputType.TYPE_CLASS_TEXT, "label");
        notes = field(uploadFields, "Notes", "cooling, power settings, anything else",
            InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_FLAG_MULTI_LINE, "notes");
        page.addView(uploadFields);
        upload.setOnCheckedChangeListener((b, on) -> {
            uploadFields.setVisibility(on ? View.VISIBLE : View.GONE);
            runButton.setText(on ? "Run and upload" : "Run");
        });

        Button advanced = new Button(this, null, android.R.attr.borderlessButtonStyle);
        advanced.setText("Advanced");
        advanced.setGravity(Gravity.START | Gravity.CENTER_VERTICAL);
        page.addView(advanced);
        advancedFields = column();
        advancedFields.setVisibility(View.GONE);
        threads = field(advancedFields, "Threads (blank: one per CPU)", "auto",
            InputType.TYPE_CLASS_NUMBER, null);
        cpus = field(advancedFields, "CPUs (0-3,6 means CPUs 0 to 3 and CPU 6)", "all",
            InputType.TYPE_CLASS_TEXT, null);
        seconds = field(advancedFields, "Seconds per measurement", "",
            InputType.TYPE_CLASS_NUMBER | InputType.TYPE_NUMBER_FLAG_DECIMAL, null);
        seconds.setText("0.5");
        reps = field(advancedFields, "Repetitions (the best is kept)", "",
            InputType.TYPE_CLASS_NUMBER, null);
        reps.setText("3");
        warmup = field(advancedFields, "Warm-up seconds", "",
            InputType.TYPE_CLASS_NUMBER | InputType.TYPE_NUMBER_FLAG_DECIMAL, null);
        warmup.setText("0.15");
        page.addView(advancedFields);
        advanced.setOnClickListener(v -> advancedFields.setVisibility(
            advancedFields.getVisibility() == View.VISIBLE ? View.GONE : View.VISIBLE));

        LinearLayout buttons = new LinearLayout(this);
        runButton = new Button(this);
        runButton.setText("Run");
        runButton.setOnClickListener(v -> run());
        stopButton = new Button(this);
        stopButton.setText("Stop");
        stopButton.setVisibility(View.GONE);
        stopButton.setOnClickListener(v -> stop());
        buttons.addView(runButton);
        buttons.addView(stopButton);
        page.addView(buttons, spaced());

        estimate = text(14, false);
        page.addView(estimate);
        command = text(12, false);
        command.setTypeface(Typeface.MONOSPACE);
        command.setTextIsSelectable(true);
        page.addView(command);
        progress = new ProgressBar(this, null, android.R.attr.progressBarStyleHorizontal);
        progress.setMax(1000);
        progress.setVisibility(View.GONE);
        page.addView(progress);
        status = text(14, false);
        status.setText("Keep the screen on this app while it measures: Android moves an "
            + "app in the background to fewer, slower cores.");
        page.addView(status);

        results = column();
        page.addView(results, spaced());

        LinearLayout actions = new LinearLayout(this);
        logButton = new Button(this, null, android.R.attr.borderlessButtonStyle);
        logButton.setText("Log");
        logButton.setOnClickListener(v -> log.setVisibility(
            log.getVisibility() == View.VISIBLE ? View.GONE : View.VISIBLE));
        saveButton = new Button(this, null, android.R.attr.borderlessButtonStyle);
        saveButton.setText("Save JSON");
        saveButton.setOnClickListener(v -> saveAs());
        shareButton = new Button(this, null, android.R.attr.borderlessButtonStyle);
        shareButton.setText("Share");
        shareButton.setOnClickListener(v -> share());
        actions.addView(logButton);
        actions.addView(saveButton);
        actions.addView(shareButton);
        saveButton.setEnabled(false);
        shareButton.setEnabled(false);
        page.addView(actions);
        log = text(11, false);
        log.setTypeface(Typeface.MONOSPACE);
        log.setTextIsSelectable(true);
        log.setVisibility(View.GONE);
        page.addView(log);

        uploadFields.setVisibility(upload.isChecked() ? View.VISIBLE : View.GONE);
        runButton.setText(upload.isChecked() ? "Run and upload" : "Run");
        for (EditText e : new EditText[] {threads, cpus, seconds, reps, warmup}) {
            e.addTextChangedListener(new SimpleWatcher(this::updateCommand));
        }
        modes.setOnCheckedChangeListener((g, id) -> updateCommand());
        variants.setOnCheckedChangeListener((g, id) -> updateCommand());
        updateCommand();
    }

    @Override
    protected void onPause() {
        super.onPause();
        if (proc != null && !leftDuringRun) {
            leftDuringRun = true;
            appendLog("\nthe app left the screen during the run: Android may have moved "
                + "it to fewer, slower cores, or paused it, so these numbers may be low\n");
        }
    }

    @Override
    protected void onDestroy() {
        super.onDestroy();
        if (proc != null) proc.destroyForcibly();
    }

    // -- the command line ------------------------------------------------

    private String binary() {
        return getApplicationInfo().nativeLibraryDir + "/libcpcpub.so";
    }

    private int choice(RadioGroup group) {
        return group.indexOfChild(group.findViewById(group.getCheckedRadioButtonId()));
    }

    private List<String> argv() {
        List<String> argv = new ArrayList<>();
        argv.add(binary());
        int v = choice(variants);
        if (v == 1) argv.add("--variants=all");
        int m = choice(modes);
        if (m == 0) argv.add("--full");
        else if (m == 2) argv.add("--per-core");
        String t = threads.getText().toString().trim();
        if (!t.isEmpty()) { argv.add("--threads"); argv.add(t); }
        String c = cpus.getText().toString().trim();
        if (!c.isEmpty()) { argv.add("--cpus"); argv.add(c); }
        argv.add("--time"); argv.add(number(seconds, "0.5"));
        argv.add("--reps"); argv.add(number(reps, "3"));
        argv.add("--warmup"); argv.add(number(warmup, "0.15"));
        argv.add("--json");
        return argv;
    }

    private static String number(EditText e, String fallback) {
        String s = e.getText().toString().trim();
        return s.isEmpty() ? fallback : s;
    }

    private static double parse(String s, double fallback) {
        try {
            return Double.parseDouble(s);
        } catch (NumberFormatException e) {
            return fallback;
        }
    }

    /** How long the settings should take: arithmetic on the flags, as on the desktop. */
    private double estimateSeconds() {
        double perPass = WARMUPS_PER_PASS * parse(number(warmup, "0.15"), 0.15)
            + PHASES_PER_PASS * parse(number(reps, "3"), 3) * parse(number(seconds, "0.5"), 0.5)
            + SETUP_SECONDS_PER_PASS;
        int n = countCpus(cpus.getText().toString());
        if (n <= 0) n = (int) parse(threads.getText().toString().trim(), 0);
        if (n <= 0) n = Runtime.getRuntime().availableProcessors();
        int m = choice(modes);
        int passes = m == 0 ? 1 + n : m == 2 ? n : 1;
        // Every variant runs the whole thing again.
        int times = choice(variants) == 0 ? 1 : 4;
        return perPass * passes * times;
    }

    private static int countCpus(String list) {
        int count = 0;
        for (String part : list.split(",")) {
            part = part.trim();
            if (part.isEmpty()) continue;
            String[] ends = part.split("-");
            try {
                int lo = Integer.parseInt(ends[0].trim());
                int hi = ends.length > 1 ? Integer.parseInt(ends[1].trim()) : lo;
                count += Math.max(0, hi - lo + 1);
            } catch (NumberFormatException e) {
                return 0;
            }
        }
        return count;
    }

    private void updateCommand() {
        List<String> argv = argv();
        command.setText("cpcpub " + TextUtils.join(" ", argv.subList(1, argv.size())));
        double s = estimateSeconds();
        estimate.setText("Estimated run time: " + (s < 90 ? Math.round(s) + " s"
            : String.format(Locale.getDefault(), "%.1f min", s / 60)));
    }

    // -- running ---------------------------------------------------------

    private void run() {
        if (proc != null || uploading) return;
        File bin = new File(binary());
        if (!bin.canExecute()) {
            status.setText("The benchmark is missing from this install: " + bin);
            return;
        }
        prefs.edit()
            .putInt("mode", choice(modes)).putInt("variants", choice(variants))
            .putBoolean("upload", upload.isChecked())
            .putString("hub", hub.getText().toString())
            .putString("token", token.getText().toString())
            .putString("label", label.getText().toString())
            .putString("notes", notes.getText().toString())
            .apply();

        List<String> argv = argv();
        try {
            ProcessBuilder pb = new ProcessBuilder(argv);
            pb.directory(getCacheDir());
            proc = pb.start();
            proc.getOutputStream().close();
        } catch (IOException e) {
            proc = null;
            status.setText("Could not start the benchmark: " + e.getMessage());
            return;
        }
        log.setText("$ " + command.getText() + "\n\n");
        results.removeAllViews();
        saveButton.setEnabled(false);
        shareButton.setEnabled(false);
        leftDuringRun = false;
        started = System.nanoTime();
        estimated = Math.max(1.0, estimateSeconds());
        runButton.setEnabled(false);
        stopButton.setVisibility(View.VISIBLE);
        progress.setVisibility(View.VISIBLE);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        main.post(tick);

        Process p = proc;
        // One thread per stream: a pipe nobody reads fills, and the benchmark
        // then stops dead in the middle of a write.
        Thread err = new Thread(() -> {
            try (BufferedReader r = new BufferedReader(new InputStreamReader(
                    p.getErrorStream(), StandardCharsets.UTF_8))) {
                for (String line; (line = r.readLine()) != null; ) {
                    String l = line;
                    main.post(() -> appendLog(l + "\n"));
                }
            } catch (IOException ignored) {
                // The process went away; its exit status says how.
            }
        });
        err.start();
        new Thread(() -> {
            StringBuilder out = new StringBuilder();
            try (BufferedReader r = new BufferedReader(new InputStreamReader(
                    p.getInputStream(), StandardCharsets.UTF_8))) {
                for (String line; (line = r.readLine()) != null; ) out.append(line).append('\n');
            } catch (IOException ignored) {
                // As above.
            }
            int code;
            try {
                code = p.waitFor();
                err.join();
            } catch (InterruptedException e) {
                code = -1;
            }
            int exit = code;
            main.post(() -> finished(exit, out.toString()));
        }).start();
    }

    private final Runnable tick = new Runnable() {
        @Override
        public void run() {
            if (proc == null) return;
            double elapsed = (System.nanoTime() - started) / 1e9;
            // Held short of full: the benchmark says nothing until it is done.
            progress.setProgress((int) (1000 * Math.min(0.99, elapsed / estimated)));
            status.setText(String.format(Locale.getDefault(), "Running: %d s of about %d s",
                (int) elapsed, Math.round(estimated)));
            main.postDelayed(this, 500);
        }
    };

    private void stop() {
        if (proc != null) {
            appendLog("\nstopped\n");
            proc.destroyForcibly();
        }
    }

    private void finished(int exit, String raw) {
        proc = null;
        main.removeCallbacks(tick);
        getWindow().clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        progress.setVisibility(View.GONE);
        stopButton.setVisibility(View.GONE);
        runButton.setEnabled(true);
        long elapsed = Math.round((System.nanoTime() - started) / 1e9);

        List<JSONObject> docs = new ArrayList<>();
        if (!raw.trim().isEmpty()) {
            try {
                Object parsed = new JSONTokener(raw).nextValue();
                if (parsed instanceof JSONArray) {
                    JSONArray a = (JSONArray) parsed;
                    for (int i = 0; i < a.length(); i++) docs.add(a.getJSONObject(i));
                } else if (parsed instanceof JSONObject) {
                    docs.add((JSONObject) parsed);
                }
            } catch (JSONException e) {
                appendLog("\nthe result document did not parse: " + e.getMessage() + "\n");
            }
        }
        if (docs.isEmpty()) {
            status.setText(exit == 0 ? "Finished, but produced no result."
                : "Exit status " + exit + " after " + elapsed + " s -- see the log.");
            log.setVisibility(View.VISIBLE);
            return;
        }
        lastRaw = raw;
        lastName = "cpcpub-" + new SimpleDateFormat("yyyyMMdd-HHmmss", Locale.ROOT)
            .format(new Date()) + ".json";
        String saved = keep(raw);
        showResults(docs, 0);
        saveButton.setEnabled(true);
        shareButton.setEnabled(true);
        status.setText("Done in " + elapsed + " s" + (saved != null ? "; kept as " + saved : ""));
        if (exit != 0 || leftDuringRun) log.setVisibility(View.VISIBLE);
        if (exit == 0 && upload.isChecked()) submit(raw, choice(variants) != 0);
    }

    /** Every result is kept in the app's own folder, as the desktop saves to a folder. */
    private String keep(String raw) {
        File dir = getExternalFilesDir("results");
        if (dir == null) return null;
        File f = new File(dir, lastName);
        try (OutputStream out = new FileOutputStream(f)) {
            out.write(raw.getBytes(StandardCharsets.UTF_8));
        } catch (IOException e) {
            appendLog("\ncould not write " + f + ": " + e.getMessage() + "\n");
            return null;
        }
        appendLog("\nsaved: " + f + "\n");
        return "Android/data/" + getPackageName() + "/files/results/" + lastName;
    }

    // -- uploading -------------------------------------------------------

    private void submit(String raw, boolean variantRun) {
        String url = hub.getText().toString().trim();
        if (url.isEmpty()) url = getString(R.string.hub_url);
        if (url.isEmpty()) {
            appendLog("\nno hub URL to upload to\n");
            log.setVisibility(View.VISIBLE);
            return;
        }
        String hubUrl = url;
        String tok = token.getText().toString();
        String lab = label.getText().toString().trim();
        String not = notes.getText().toString().trim();
        List<String> docs = Hub.documents(raw);
        uploading = true;
        runButton.setEnabled(false);
        status.setText("Uploading ...");
        // The reply carries the delete token an anonymous upload needs to
        // withdraw itself, and it is shown nowhere but the log.
        log.setVisibility(View.VISIBLE);
        new Thread(() -> {
            int landed = 0;
            for (String doc : docs) {
                String variant = null;
                if (variantRun) {
                    try {
                        variant = Metrics.variantOf(new JSONObject(doc));
                    } catch (JSONException ignored) {
                        // Uploaded without a tag; the hub reads the build flags anyway.
                    }
                }
                boolean[] ok = new boolean[1];
                String said = Hub.submit(hubUrl, tok, lab, not, variant, doc, ok);
                if (ok[0]) landed++;
                main.post(() -> appendLog(said));
            }
            int n = landed;
            main.post(() -> {
                uploading = false;
                runButton.setEnabled(true);
                status.setText(n == docs.size() ? "Uploaded. Copy the delete token from "
                    + "the log if you may want to withdraw it."
                    : "The upload did not land -- see the log.");
            });
        }).start();
    }

    // -- the results -----------------------------------------------------

    private void showResults(List<JSONObject> docs, int which) {
        results.removeAllViews();
        if (docs.size() > 1) {
            HorizontalScrollView strip = new HorizontalScrollView(this);
            LinearLayout tabs = new LinearLayout(this);
            for (int i = 0; i < docs.size(); i++) {
                Button b = new Button(this, null, android.R.attr.borderlessButtonStyle);
                b.setAllCaps(false);
                b.setText(Metrics.variantOf(docs.get(i)));
                // The page on show reads as chosen, the others as choices.
                b.setTypeface(i == which ? Typeface.DEFAULT_BOLD : Typeface.DEFAULT);
                b.setAlpha(i == which ? 1f : 0.6f);
                int index = i;
                b.setOnClickListener(v -> showResults(docs, index));
                tabs.addView(b);
            }
            strip.addView(tabs);
            results.addView(strip);
        }
        JSONObject doc = docs.get(which);
        JSONObject sys = doc.optJSONObject("system");
        TextView cpu = text(18, true);
        cpu.setText(sys != null ? sys.optString("cpu_models", "(cpu not named)") : "");
        results.addView(cpu);

        TableLayout table = new TableLayout(this);
        TableRow head = new TableRow(this);
        head.addView(cell("", false, false));
        for (String[] col : Metrics.COLUMNS) {
            TextView t = cell(col[1] + "\n" + col[2], true, true);
            t.setOnClickListener(v -> explain(col[1], Metrics.EXPLAIN.get(col[0])));
            head.addView(t);
        }
        table.addView(head);
        for (Object[] scope : Metrics.scopes(doc)) {
            String name = (String) scope[0];
            JSONObject rec = (JSONObject) scope[1];
            TableRow row = new TableRow(this);
            TextView n = cell(name, name.equals("total"), false);
            n.setOnClickListener(v -> explain(name, Metrics.explainScope(name)));
            row.addView(n);
            for (String[] col : Metrics.COLUMNS) {
                String key = col[0];
                String shown = Metrics.format(rec.opt(key));
                String src = rec.optString("mhz_src", "");
                if (key.equals("mhz") && src.equals("estimated")) shown = "~" + shown;
                TextView t = cell(shown, Metrics.SCORE_PARTS.contains(key), true);
                if (key.equals("mhz")) {
                    String why = Metrics.CLOCK_SOURCES.get(src);
                    t.setOnClickListener(v -> explain("clock", why != null ? why
                        : "No clock was reported for this core."));
                }
                row.addView(t);
            }
            table.addView(row);
        }
        HorizontalScrollView wide = new HorizontalScrollView(this);
        wide.addView(table);
        results.addView(wide);
        results.addView(note("Tap a heading for what it measures. The score and the "
            + "columns in bold are what it is a geometric mean of."));
    }

    private void explain(String title, String text) {
        new AlertDialog.Builder(this).setTitle(title).setMessage(text)
            .setPositiveButton(android.R.string.ok, null).show();
    }

    // -- saving and sharing ----------------------------------------------

    private void saveAs() {
        if (lastRaw == null) return;
        Intent i = new Intent(Intent.ACTION_CREATE_DOCUMENT);
        i.addCategory(Intent.CATEGORY_OPENABLE);
        i.setType("application/json");
        i.putExtra(Intent.EXTRA_TITLE, lastName);
        startActivityForResult(i, SAVE_REQUEST);
    }

    @Override
    protected void onActivityResult(int request, int result, Intent data) {
        super.onActivityResult(request, result, data);
        if (request != SAVE_REQUEST || result != RESULT_OK || data == null) return;
        Uri uri = data.getData();
        if (uri == null || lastRaw == null) return;
        try (OutputStream out = getContentResolver().openOutputStream(uri)) {
            if (out == null) throw new IOException("no stream for " + uri);
            out.write(lastRaw.getBytes(StandardCharsets.UTF_8));
            status.setText("Saved.");
        } catch (IOException e) {
            status.setText("Could not save: " + e.getMessage());
        }
    }

    private void share() {
        if (lastRaw == null) return;
        Intent i = new Intent(Intent.ACTION_SEND);
        i.setType("text/plain");
        i.putExtra(Intent.EXTRA_SUBJECT, lastName);
        i.putExtra(Intent.EXTRA_TEXT, lastRaw);
        startActivity(Intent.createChooser(i, "Share the result"));
    }

    // -- small pieces ----------------------------------------------------

    private void appendLog(String s) {
        log.append(s);
    }

    private int dp(int v) {
        return Math.round(TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_DIP, v,
            getResources().getDisplayMetrics()));
    }

    private LinearLayout.LayoutParams spaced() {
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        lp.topMargin = dp(12);
        return lp;
    }

    private LinearLayout column() {
        LinearLayout l = new LinearLayout(this);
        l.setOrientation(LinearLayout.VERTICAL);
        return l;
    }

    private TextView text(int sp, boolean bold) {
        TextView t = new TextView(this);
        t.setTextSize(TypedValue.COMPLEX_UNIT_SP, sp);
        if (bold) t.setTypeface(Typeface.DEFAULT_BOLD);
        return t;
    }

    private TextView heading(String s) {
        TextView t = text(16, true);
        t.setText(s);
        t.setPadding(0, dp(12), 0, dp(2));
        return t;
    }

    private TextView note(String s) {
        TextView t = text(12, false);
        t.setText(s);
        t.setAlpha(0.7f);
        return t;
    }

    private RadioGroup radios(LinearLayout parent, String[] labels, int checked) {
        RadioGroup g = new RadioGroup(this);
        for (int i = 0; i < labels.length; i++) {
            RadioButton r = new RadioButton(this);
            r.setId(View.generateViewId());
            r.setText(labels[i]);
            g.addView(r);
            if (i == checked) g.check(r.getId());
        }
        if (g.getCheckedRadioButtonId() == View.NO_ID) g.check(g.getChildAt(0).getId());
        parent.addView(g);
        return g;
    }

    private EditText field(LinearLayout parent, String title, String hint, int type,
                           String pref) {
        TextView t = text(12, false);
        t.setText(title);
        t.setPadding(0, dp(8), 0, 0);
        parent.addView(t);
        EditText e = new EditText(this);
        e.setHint(hint);
        e.setInputType(type);
        e.setSingleLine((type & InputType.TYPE_TEXT_FLAG_MULTI_LINE) == 0);
        if (pref != null) e.setText(prefs.getString(pref, ""));
        parent.addView(e);
        return e;
    }

    private TextView cell(String s, boolean bold, boolean number) {
        TextView t = text(13, bold);
        t.setText(s);
        t.setPadding(dp(6), dp(4), dp(6), dp(4));
        t.setGravity(number ? Gravity.END : Gravity.START);
        return t;
    }
}
