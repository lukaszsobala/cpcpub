package je.qd.cpcpub;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.Intent;
import android.content.IntentFilter;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.graphics.Insets;
import android.graphics.Typeface;
import android.net.Uri;
import android.os.BatteryManager;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.os.PowerManager;
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
import android.widget.ImageView;
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
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStreamReader;
import java.io.InputStream;
import java.io.OutputStream;
import java.nio.charset.StandardCharsets;
import java.text.SimpleDateFormat;
import java.util.ArrayList;
import java.util.Arrays;
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
    private static final int NOTIFY_REQUEST = 2;
    // The cool-down choices, in seconds, and the one a new install starts on:
    // a phone heats in the half minute of the multi-threaded run, and starts
    // the per-core sweep throttled unless it is let cool first.
    private static final int[] COOLDOWNS = {0, 30, 60, 120};
    private static final String[] COOLDOWN_NAMES = {"Off", "30 s", "1 min", "2 min"};
    private static final int COOLDOWN_DEFAULT = 1;
    // When to say before a run that the phone is likely still warm: a battery
    // this hot, or a run that ended this recently. A phone idles in the low
    // thirties and comes out of a run near forty.
    private static final float WARM_BATTERY_C = 45f;
    private static final long RECENT_RUN_MS = 3 * 60 * 1000;
    // PowerManager's thermal statuses, by their number, in words.
    private static final String[] THERMAL = {
        "not throttling", "throttling lightly", "throttling", "throttling hard",
        "throttling very hard", "close to shutting down from heat", "shutting down from heat"};
    // How many past results the list offers, newest first.
    private static final int PAST_SHOWN = 100;

    private final Handler main = new Handler(Looper.getMainLooper());
    private SharedPreferences prefs;

    private CheckBox both;
    private CheckBox multi;
    private CheckBox perCore;
    private boolean syncingModes;  // set while the code, not the user, ticks them
    private RadioGroup variants;
    private RadioGroup cooldown;
    private TextView cooldownNote;
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
    private TextView status;
    private Button compareButton;
    private Button withdrawButton;
    private TextView warning;
    private ScrollView scroll;
    private LinearLayout results;
    private Button logButton;
    private Button saveButton;
    private Button shareButton;
    private TextView log;

    private Process proc;
    private long started;
    private long coolUntil;  // System.nanoTime() when the benchmark's rest ends
    private double estimated;
    private boolean leftDuringRun;
    private boolean stopped;
    private boolean uploading;
    private PowerManager power;
    private PowerManager.OnThermalStatusChangedListener thermalListener;
    private int worstThermal;         // the hottest Android said it ran during this run
    private boolean saverDuringRun;
    private float batteryAtStart;
    private String lastRaw;
    private String lastName;

    // -- the screen ------------------------------------------------------

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        prefs = getSharedPreferences("form", MODE_PRIVATE);
        power = getSystemService(PowerManager.class);

        LinearLayout root = column();
        View header = header();
        root.addView(header);
        scroll = new ScrollView(this);
        LinearLayout page = new LinearLayout(this);
        page.setOrientation(LinearLayout.VERTICAL);
        int pad = dp(16);
        page.setPadding(pad, dp(8), pad, pad);
        scroll.addView(page);
        root.addView(scroll, new LinearLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f));
        setContentView(root);
        // Android 15 draws an app under the status and navigation bars unless
        // the app moves out of their way; earlier versions report no insets.
        // The header reaches up behind the status bar, in its colour.
        if (Build.VERSION.SDK_INT >= 30) {
            int headerTop = header.getPaddingTop();
            root.setOnApplyWindowInsetsListener((v, insets) -> {
                Insets bars = insets.getInsets(
                    WindowInsets.Type.systemBars() | WindowInsets.Type.ime());
                header.setPadding(bars.left + pad, bars.top + headerTop,
                    bars.right + pad, header.getPaddingBottom());
                scroll.setPadding(bars.left, 0, bars.right, bars.bottom);
                return WindowInsets.CONSUMED;
            });
        }

        page.addView(heading("Run"));
        // As in the desktop window: Both sums up the two under it, and a run
        // needs at least one of them.
        int mode = prefs.getInt("mode", 0);
        both = check(page, "Both", 0);
        multi = check(page, "Multi-threaded: all threads at once", dp(28));
        perCore = check(page, "Per-core: each core on its own", dp(28));
        multi.setChecked(mode != 2);
        perCore.setChecked(mode != 1);
        both.setChecked(mode == 0);
        page.addView(note("Uploading both is most useful for comparisons."));

        page.addView(heading("Variants"));
        // Two choices, not the desktop's three: which variants differ is fixed by
        // the -march a binary was built for, not by the processor it runs on, and
        // the Android build's armv8-a has both a vector unit and FMA, so all four
        // of its variants are distinct and --variants would run the same four as
        // --variants=all.
        variants = radios(page, new String[] {
            "Baseline only (scalar-nofma)", "All four, and compare them"},
            prefs.getInt("variants", 0));
        page.addView(note("Compare baseline across architectures, "
            + "others only within the same architecture."));

        page.addView(heading("Cool-down"));
        cooldown = radios(page, COOLDOWN_NAMES, prefs.getInt("cooldown", COOLDOWN_DEFAULT));
        cooldown.setOrientation(LinearLayout.HORIZONTAL);
        cooldownNote = note("");
        page.addView(cooldownNote);

        upload = new CheckBox(this);
        upload.setText("Upload results");
        upload.setChecked(prefs.getBoolean("upload", false));
        page.addView(upload, spaced());
        uploadFields = column();
        token = field(uploadFields, "Token (optional, from the hub's Account tab)", "",
            InputType.TYPE_CLASS_TEXT
            | InputType.TYPE_TEXT_VARIATION_PASSWORD, "token");
        revealable(token);
        label = field(uploadFields, "Label", "short name for this phone",
            InputType.TYPE_CLASS_TEXT, "label");
        // The phone's own name until the label has been set once: the CPU line
        // names only core designs, which many phones share.
        if (!prefs.contains("label")) label.setText(deviceName());
        notes = field(uploadFields, "Notes", "cooling, power settings, anything else",
            InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_FLAG_MULTI_LINE, "notes");
        page.addView(uploadFields);
        upload.setOnCheckedChangeListener((b, on) -> {
            uploadFields.setVisibility(on ? View.VISIBLE : View.GONE);
            runButton.setText(on ? "Run and upload" : "Run");
        });

        // A disclosure rather than an action, so it stays flat, with the arrow
        // saying which way it is.
        Button advanced = new Button(this, null, android.R.attr.borderlessButtonStyle);
        advanced.setText("Advanced ▸");
        advanced.setAllCaps(false);
        advanced.setGravity(Gravity.START | Gravity.CENTER_VERTICAL);
        page.addView(advanced);
        advancedFields = column();
        advancedFields.setVisibility(View.GONE);
        threads = field(advancedFields, "Threads (blank: one per CPU)", "auto",
            InputType.TYPE_CLASS_NUMBER, null);
        cpus = field(advancedFields, "CPUs (e.g. 0-3,6)", "all",
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
        // Blank is the hub the release was built with, shown as the hint.
        hub = field(advancedFields, "Hub URL", getString(R.string.hub_url),
            InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_URI, "hub");
        page.addView(advancedFields);
        advanced.setOnClickListener(v -> {
            boolean open = advancedFields.getVisibility() != View.VISIBLE;
            advancedFields.setVisibility(open ? View.VISIBLE : View.GONE);
            advanced.setText(open ? "Advanced ▾" : "Advanced ▸");
        });

        LinearLayout buttons = new LinearLayout(this);
        runButton = button(buttons, "Run");
        runButton.setOnClickListener(v -> run());
        stopButton = button(buttons, "Stop");
        stopButton.setVisibility(View.GONE);
        stopButton.setOnClickListener(v -> stop());
        button(buttons, "Past results").setOnClickListener(v -> showPast());
        page.addView(buttons, spaced());

        estimate = text(14, false);
        page.addView(estimate);
        progress = new ProgressBar(this, null, android.R.attr.progressBarStyleHorizontal);
        progress.setMax(1000);
        progress.setVisibility(View.GONE);
        page.addView(progress);
        status = text(14, false);
        status.setText("Stay in the app while it measures. "
            + "In the background it may get slower cores.");
        page.addView(status);
        // What may have held this run's numbers down, in words.
        warning = text(14, false);
        warning.setTextColor(0xFFD9822B);
        warning.setVisibility(View.GONE);
        page.addView(warning);
        // After an upload: the run's page on the hub, where it is compared.
        LinearLayout hubButtons = new LinearLayout(this);
        compareButton = button(hubButtons, "See how it compares");
        compareButton.setVisibility(View.GONE);
        withdrawButton = button(hubButtons, "Withdraw");
        withdrawButton.setVisibility(View.GONE);
        page.addView(hubButtons);

        results = column();
        page.addView(results, spaced());

        LinearLayout actions = new LinearLayout(this);
        logButton = button(actions, "Log");
        logButton.setOnClickListener(v -> log.setVisibility(
            log.getVisibility() == View.VISIBLE ? View.GONE : View.VISIBLE));
        saveButton = button(actions, "Save JSON");
        saveButton.setOnClickListener(v -> saveAs());
        shareButton = button(actions, "Share");
        shareButton.setOnClickListener(v -> share());
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
        both.setOnCheckedChangeListener((b, on) -> {
            if (syncingModes) return;
            // Unticked, it goes back to the multi-threaded run alone.
            setModes(true, on);
        });
        multi.setOnCheckedChangeListener((b, on) -> onModeChecked(perCore));
        perCore.setOnCheckedChangeListener((b, on) -> onModeChecked(multi));
        variants.setOnCheckedChangeListener((g, id) -> updateCommand());
        cooldown.setOnCheckedChangeListener((g, id) -> updateCommand());
        updateCommand();
    }

    @Override
    protected void onPause() {
        super.onPause();
        if (proc != null && !leftDuringRun) {
            leftDuringRun = true;
            appendLog("\nthe app left the screen during the run; the numbers may be low\n");
        }
    }

    @Override
    protected void onDestroy() {
        super.onDestroy();
        if (proc != null) proc.destroyForcibly();
        stopService(new Intent(this, RunService.class));
    }

    @Override
    public void onRequestPermissionsResult(int request, String[] permissions, int[] granted) {
        super.onRequestPermissionsResult(request, permissions, granted);
        // Granted or not, the run goes ahead: the permission only shows the
        // notification while it measures.
        if (request == NOTIFY_REQUEST) checkThenStart();
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
        int m = mode();
        if (m == 0) argv.add("--full");
        else if (m == 2) argv.add("--per-core");
        String t = threads.getText().toString().trim();
        if (!t.isEmpty()) { argv.add("--threads"); argv.add(t); }
        String c = cpus.getText().toString().trim();
        if (!c.isEmpty()) { argv.add("--cpus"); argv.add(c); }
        argv.add("--time"); argv.add(number(seconds, "0.5"));
        argv.add("--reps"); argv.add(number(reps, "3"));
        argv.add("--warmup"); argv.add(number(warmup, "0.15"));
        if (rests() > 0 && cooldownSeconds() > 0) {
            argv.add("--cooldown"); argv.add(Integer.toString(cooldownSeconds()));
        }
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
        int m = mode();
        int passes = m == 0 ? 1 + n : m == 2 ? n : 1;
        // Every variant runs the whole thing again.
        int times = choice(variants) == 0 ? 1 : 4;
        return perPass * passes * times + rests() * cooldownSeconds();
    }

    private int cooldownSeconds() {
        int i = choice(cooldown);
        return i >= 0 && i < COOLDOWNS.length ? COOLDOWNS[i] : 0;
    }

    /** How many rests --cooldown takes: one before each batch but the first, a
     *  batch being a multi-threaded run or a whole per-core sweep. */
    private int rests() {
        int batches = (mode() == 0 ? 2 : 1) * (choice(variants) == 0 ? 1 : 4);
        return batches - 1;
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
        // A run of one batch has nothing to rest between: the choice is shown
        // greyed rather than taken away, with the reason under it.
        boolean applies = rests() > 0;
        for (int i = 0; i < cooldown.getChildCount(); i++) {
            cooldown.getChildAt(i).setEnabled(applies);
        }
        cooldownNote.setText(applies
            ? "A rest between runs, so each starts on a cool phone."
            : "Needs Both or all four variants.");
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
            .putInt("mode", mode()).putInt("variants", choice(variants))
            .putInt("cooldown", choice(cooldown))
            .putBoolean("upload", upload.isChecked())
            .putString("hub", hub.getText().toString())
            .putString("token", token.getText().toString())
            .putString("label", label.getText().toString())
            .putString("notes", notes.getText().toString())
            .apply();

        // The notification that says a run is going, for when the app is
        // left: asked for once, before the first run, rather than mid-run,
        // where the dialog would count as leaving the screen.
        if (Build.VERSION.SDK_INT >= 33 && !prefs.getBoolean("askedNotify", false)
                && checkSelfPermission(android.Manifest.permission.POST_NOTIFICATIONS)
                    != PackageManager.PERMISSION_GRANTED) {
            prefs.edit().putBoolean("askedNotify", true).apply();
            requestPermissions(new String[] {android.Manifest.permission.POST_NOTIFICATIONS},
                NOTIFY_REQUEST);
            return;
        }
        checkThenStart();
    }

    private void checkThenStart() {
        if (proc != null || uploading) return;
        // Whatever would hold the numbers down, said before the run rather
        // than found out after it; the run is still the user's to start.
        List<String> why = beforeRun();
        if (why.isEmpty()) {
            start();
            return;
        }
        new AlertDialog.Builder(this)
            .setTitle("Results may be low")
            .setMessage(TextUtils.join("\n", why))
            .setPositiveButton("Run anyway", (d, w) -> start())
            .setNegativeButton("Not now", null)
            .show();
    }

    /** What would make a run started now measure low, in words. */
    private List<String> beforeRun() {
        List<String> why = new ArrayList<>();
        if (power.isPowerSaveMode()) {
            why.add("Battery saver is on.");
        }
        int thermal = power.getCurrentThermalStatus();
        if (thermal >= PowerManager.THERMAL_STATUS_LIGHT) {
            why.add("The phone is " + thermalName(thermal) + ".");
        }
        float battery = batteryTemp();
        if (battery >= WARM_BATTERY_C) {
            why.add(String.format(Locale.getDefault(), "The battery is at %.0f °C.", battery));
        }
        long since = System.currentTimeMillis() - prefs.getLong("lastRunEnd", 0);
        if (since >= 0 && since < RECENT_RUN_MS) {
            why.add("The last run ended " + since / 1000 + " s ago.");
        }
        return why;
    }

    private void start() {
        if (proc != null || uploading) return;
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
        // The command, for the log only: what ran, should a number look odd.
        log.setText("$ cpcpub " + TextUtils.join(" ", argv.subList(1, argv.size())) + "\n\n");
        results.removeAllViews();
        saveButton.setEnabled(false);
        shareButton.setEnabled(false);
        leftDuringRun = false;
        stopped = false;
        compareButton.setVisibility(View.GONE);
        withdrawButton.setVisibility(View.GONE);
        warning.setVisibility(View.GONE);
        // Android's own word on the heat, followed through the run: the
        // hottest it gets is what the numbers were measured at.
        worstThermal = power.getCurrentThermalStatus();
        saverDuringRun = power.isPowerSaveMode();
        batteryAtStart = batteryTemp();
        thermalListener = st -> worstThermal = Math.max(worstThermal, st);
        power.addThermalStatusListener(getMainExecutor(), thermalListener);
        started = System.nanoTime();
        coolUntil = 0;
        estimated = Math.max(1.0, estimateSeconds());
        runButton.setEnabled(false);
        stopButton.setVisibility(View.VISIBLE);
        progress.setVisibility(View.VISIBLE);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        try {
            startForegroundService(new Intent(this, RunService.class));
        } catch (RuntimeException e) {
            // Refused (a phone that limits it, say): the run goes on without it,
            // and leaving the app is then flagged as it always was.
            appendLog("could not keep the run going in the background: " + e.getMessage() + "\n");
        }
        main.post(tick);

        Process p = proc;
        // One thread per stream: a pipe nobody reads fills, and the benchmark
        // then stops dead in the middle of a write.
        Thread err = new Thread(() -> {
            try (BufferedReader r = new BufferedReader(new InputStreamReader(
                    p.getErrorStream(), StandardCharsets.UTF_8))) {
                for (String line; (line = r.readLine()) != null; ) {
                    String l = line;
                    main.post(() -> onErrLine(l));
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
            long left = (coolUntil - System.nanoTime()) / 1_000_000_000L;
            status.setText(coolUntil > System.nanoTime()
                ? String.format(Locale.getDefault(),
                    "Cooling down, %d s left (%d s of about %d s)", left + 1,
                    (int) elapsed, Math.round(estimated))
                : String.format(Locale.getDefault(), "Running: %d s of about %d s",
                    (int) elapsed, Math.round(estimated)));
            main.postDelayed(this, 500);
        }
    };

    private void onErrLine(String line) {
        appendLog(line + "\n");
        // The benchmark says when it starts to rest, and for how long.
        if (line.startsWith("cooling down for ")) {
            String[] words = line.split(" ");
            double secs = words.length > 3 ? parse(words[3], 0) : 0;
            coolUntil = System.nanoTime() + (long) (secs * 1e9);
        }
    }

    private void stop() {
        if (proc != null) {
            appendLog("\nstopped\n");
            stopped = true;
            proc.destroyForcibly();
        }
    }

    private void finished(int exit, String raw) {
        proc = null;
        main.removeCallbacks(tick);
        afterRun();
        getWindow().clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        stopService(new Intent(this, RunService.class));
        progress.setVisibility(View.GONE);
        stopButton.setVisibility(View.GONE);
        runButton.setEnabled(true);
        long elapsed = Math.round((System.nanoTime() - started) / 1e9);

        List<JSONObject> docs = parseDocs(raw);
        if (docs.isEmpty()) {
            if (stopped) {
                status.setText("Stopped after " + elapsed + " s.");
                return;
            }
            status.setText(exit == 0 ? "Finished, but produced no result."
                : "Failed after " + elapsed + " s; see the log.");
            log.setVisibility(View.VISIBLE);
            return;
        }
        lastRaw = raw;
        lastName = "cpcpub-" + new SimpleDateFormat("yyyyMMdd-HHmmss", Locale.ROOT)
            .format(new Date()) + ".json";
        keep(raw);
        showResults(docs, 0);
        saveButton.setEnabled(true);
        shareButton.setEnabled(true);
        status.setText("Done in " + elapsed + " s.");
        if (exit != 0) log.setVisibility(View.VISIBLE);
        // The score is below the form; bring it up rather than leave it to be
        // scrolled to.
        scroll.post(() -> scroll.smoothScrollTo(0, Math.max(0, status.getTop() - dp(8))));
        if (exit == 0 && upload.isChecked()) submit(raw, choice(variants) != 0, lastName);
    }

    /** The documents in what the benchmark printed: one, or a --variants array. */
    private List<JSONObject> parseDocs(String raw) {
        List<JSONObject> docs = new ArrayList<>();
        if (raw.trim().isEmpty()) return docs;
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
        return docs;
    }

    /** The heat and the battery saver over the run, in the log, and as a warning
     *  where they will have held the numbers down. */
    private void afterRun() {
        if (thermalListener != null) {
            power.removeThermalStatusListener(thermalListener);
            thermalListener = null;
        }
        prefs.edit().putLong("lastRunEnd", System.currentTimeMillis()).apply();
        saverDuringRun |= power.isPowerSaveMode();
        float end = batteryTemp();
        appendLog(String.format(Locale.US, "\nbattery %.1f °C at the start, %.1f °C at the "
            + "end; Android said the phone was %s at its hottest\n",
            batteryAtStart, end, thermalName(worstThermal)));

        List<String> why = new ArrayList<>();
        if (leftDuringRun) why.add("the app left the screen");
        if (worstThermal >= PowerManager.THERMAL_STATUS_LIGHT) {
            why.add("the phone was " + thermalName(worstThermal));
        }
        if (saverDuringRun) why.add("battery saver was on");
        if (why.isEmpty()) return;
        warning.setText("Likely low: " + TextUtils.join(", ", why) + ".");
        warning.setVisibility(View.VISIBLE);
    }

    private static String thermalName(int status) {
        return status >= 0 && status < THERMAL.length ? THERMAL[status] : "at heat level " + status;
    }

    /** The battery's temperature in °C, or NaN where the phone does not say. */
    private float batteryTemp() {
        Intent b = registerReceiver(null, new IntentFilter(Intent.ACTION_BATTERY_CHANGED));
        int t = b == null ? Integer.MIN_VALUE
            : b.getIntExtra(BatteryManager.EXTRA_TEMPERATURE, Integer.MIN_VALUE);
        return t == Integer.MIN_VALUE ? Float.NaN : t / 10f;
    }

    /** What this phone calls itself: maker and model, and the chip where it says. */
    private static String deviceName() {
        String make = Build.MANUFACTURER == null ? "" : Build.MANUFACTURER.trim();
        String model = Build.MODEL == null ? "" : Build.MODEL.trim();
        String name = make.isEmpty() || model.regionMatches(true, 0, make, 0, make.length())
            ? model : Character.toUpperCase(make.charAt(0)) + make.substring(1) + " " + model;
        if (Build.VERSION.SDK_INT >= 31) {
            String soc = Build.SOC_MODEL;
            if (soc != null && !soc.isEmpty() && !soc.equals(Build.UNKNOWN)) {
                name += " (" + soc + ")";
            }
        }
        return name.trim();
    }

    /** Every result is kept in the app's own folder, as the desktop saves to a folder. */
    private void keep(String raw) {
        File dir = getExternalFilesDir("results");
        if (dir == null) return;
        File f = new File(dir, lastName);
        try (OutputStream out = new FileOutputStream(f)) {
            out.write(raw.getBytes(StandardCharsets.UTF_8));
        } catch (IOException e) {
            appendLog("\ncould not write " + f + ": " + e.getMessage() + "\n");
            return;
        }
        appendLog("\nsaved: " + f + "\n");
    }

    // -- uploading -------------------------------------------------------

    private void submit(String raw, boolean variantRun, String name) {
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
        new Thread(() -> {
            int landed = 0;
            String page = null;
            JSONArray kept = new JSONArray();
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
                if (ok[0] && page == null) page = runPage(hubUrl, said);
                JSONObject reply = ok[0] ? reply(said) : null;
                if (reply != null) {
                    try {
                        kept.put(new JSONObject()
                            .put("hub", hubUrl).put("variant", variant)
                            .put("id", reply.opt("id"))
                            .put("delete_token", reply.optString("delete_token", ""))
                            .put("page", runPage(hubUrl, said)));
                    } catch (JSONException ignored) {
                        // Only strings and numbers go in; it cannot fail.
                    }
                }
                main.post(() -> appendLog(said));
            }
            // The delete token is shown once, by the hub: kept beside the result,
            // so the upload can be withdrawn from Past results long after.
            if (kept.length() > 0) keepReceipt(name, kept);
            int n = landed;
            String first = page;
            main.post(() -> {
                uploading = false;
                runButton.setEnabled(true);
                if (n == docs.size()) {
                    status.setText("Uploaded.");
                } else {
                    status.setText("Upload failed; see the log.");
                    log.setVisibility(View.VISIBLE);
                }
                if (first != null) {
                    compareButton.setOnClickListener(v -> openPage(first));
                    compareButton.setVisibility(View.VISIBLE);
                }
                if (name.equals(lastName)) showWithdraw(name);
            });
        }).start();
    }

    /** The hub's reply to an upload, from the log text in `said`, or null. */
    private static JSONObject reply(String said) {
        for (String line : said.split("\n")) {
            if (!line.startsWith("uploaded: ")) continue;
            try {
                return new JSONObject(line.substring("uploaded: ".length()));
            } catch (JSONException e) {
                return null;
            }
        }
        return null;
    }

    /** The uploaded run's page on the hub, from the reply in `said`, or null. */
    private static String runPage(String hub, String said) {
        JSONObject r = reply(said);
        String url = r == null ? "" : r.optString("url", "");
        if (url.startsWith("/")) url = hub.replaceAll("/+$", "") + url;
        return url.startsWith("http://") || url.startsWith("https://") ? url : null;
    }

    // -- receipts and withdrawing ----------------------------------------

    /** Where the hub's replies for the result `name` are kept: the app's own
     *  storage, which no other app can read, since a delete token withdraws. */
    private File receiptFile(String name) {
        return new File(new File(getFilesDir(), "uploads"), name);
    }

    private JSONArray readReceipt(String name) {
        File f = receiptFile(name);
        if (!f.isFile()) return new JSONArray();
        try {
            return new JSONArray(readFile(f));
        } catch (IOException | JSONException e) {
            return new JSONArray();
        }
    }

    /** Add `uploads` to what is kept for `name`. Called off the main thread too. */
    private synchronized void keepReceipt(String name, JSONArray uploads) {
        JSONArray all = readReceipt(name);
        for (int i = 0; i < uploads.length(); i++) all.put(uploads.opt(i));
        writeReceipt(name, all);
    }

    private synchronized void writeReceipt(String name, JSONArray all) {
        File f = receiptFile(name);
        File dir = f.getParentFile();
        if (dir != null && !dir.isDirectory() && !dir.mkdirs()) return;
        try (OutputStream out = new FileOutputStream(f)) {
            out.write(all.toString(2).getBytes(StandardCharsets.UTF_8));
        } catch (IOException | JSONException e) {
            main.post(() -> appendLog("\ncould not keep the upload's delete token: "
                + e.getMessage() + "\n"));
        }
    }

    /** The Withdraw button, for a result on screen with uploads still standing. */
    private void showWithdraw(String name) {
        JSONArray kept = readReceipt(name);
        int standing = 0;
        for (int i = 0; i < kept.length(); i++) {
            JSONObject r = kept.optJSONObject(i);
            if (r != null && !r.optBoolean("withdrawn") && !r.optString("delete_token").isEmpty()) {
                standing++;
            }
        }
        withdrawButton.setVisibility(standing > 0 ? View.VISIBLE : View.GONE);
        int n = standing;
        withdrawButton.setOnClickListener(v -> new AlertDialog.Builder(this)
            .setTitle("Withdraw from the hub?")
            .setMessage((n > 1 ? "All " + n + " uploads come off the hub. " : "")
                + "The result stays on the phone.")
            .setPositiveButton("Withdraw", (d, w) -> withdraw(name))
            .setNegativeButton(android.R.string.cancel, null)
            .show());
    }

    private void withdraw(String name) {
        withdrawButton.setEnabled(false);
        status.setText("Withdrawing ...");
        new Thread(() -> {
            JSONArray kept = readReceipt(name);
            int failed = 0;
            for (int i = 0; i < kept.length(); i++) {
                JSONObject r = kept.optJSONObject(i);
                if (r == null || r.optBoolean("withdrawn")) continue;
                boolean[] ok = new boolean[1];
                String said = Hub.withdraw(r.optString("hub"), r.optString("id"),
                    r.optString("delete_token"), ok);
                main.post(() -> appendLog(said));
                if (!ok[0]) {
                    failed++;
                    continue;
                }
                try {
                    r.put("withdrawn", true);
                } catch (JSONException ignored) {
                    // A boolean; it cannot fail.
                }
            }
            writeReceipt(name, kept);
            int f = failed;
            main.post(() -> {
                withdrawButton.setEnabled(true);
                if (f == 0) {
                    status.setText("Withdrawn.");
                    compareButton.setVisibility(View.GONE);
                } else {
                    status.setText("Withdraw failed; see the log.");
                    log.setVisibility(View.VISIBLE);
                }
                if (name.equals(lastName)) showWithdraw(name);
            });
        }).start();
    }

    // -- past results ----------------------------------------------------

    /** The results kept on the phone, newest first, to open one again. */
    private void showPast() {
        File dir = getExternalFilesDir("results");
        File[] files = dir == null ? null : dir.listFiles((d, n) -> n.endsWith(".json"));
        if (files == null || files.length == 0) {
            explain("Past results", "No results yet.");
            return;
        }
        // The names carry the time they were made, so their order is the clock's.
        Arrays.sort(files, (a, b) -> b.getName().compareTo(a.getName()));
        File[] shown = Arrays.copyOf(files, Math.min(files.length, PAST_SHOWN));
        new Thread(() -> {
            String[] lines = new String[shown.length];
            for (int i = 0; i < shown.length; i++) lines[i] = summary(shown[i]);
            main.post(() -> new AlertDialog.Builder(this)
                .setTitle("Past results")
                .setItems(lines, (d, which) -> openPast(shown[which]))
                .setNegativeButton(android.R.string.cancel, null)
                .show());
        }).start();
    }

    /** One line for the list: when, the score, and whether it is on a hub. */
    private String summary(File f) {
        StringBuilder line = new StringBuilder(when(f.getName()));
        try {
            Object parsed = new JSONTokener(readFile(f)).nextValue();
            JSONObject doc = parsed instanceof JSONArray ? ((JSONArray) parsed).optJSONObject(0)
                : (JSONObject) parsed;
            JSONObject total = doc == null ? null : doc.optJSONObject("total");
            if (total != null) {
                line.append(" · score ").append(Metrics.format(total.opt("score")));
            } else if (doc != null) {
                double best = 0;
                for (Object[] scope : Metrics.scopes(doc)) {
                    best = Math.max(best, ((JSONObject) scope[1]).optDouble("score", 0));
                }
                line.append(" · best core ").append(Metrics.format(best));
            }
            if (parsed instanceof JSONArray) {
                line.append(" · ").append(((JSONArray) parsed).length()).append(" variants");
            }
        } catch (IOException | JSONException | ClassCastException e) {
            line.append(" · unreadable");
        }
        JSONArray kept = readReceipt(f.getName());
        if (kept.length() > 0) {
            line.append(kept.optJSONObject(0) != null && kept.optJSONObject(0).optBoolean("withdrawn")
                ? " · withdrawn" : " · uploaded");
        }
        return line.toString();
    }

    /** cpcpub-20261006-140312.json -> "6 Oct 2026, 14:03", or the name itself. */
    private static String when(String name) {
        try {
            Date d = new SimpleDateFormat("yyyyMMdd-HHmmss", Locale.ROOT)
                .parse(name.replace("cpcpub-", "").replace(".json", ""));
            if (d != null) {
                return java.text.DateFormat.getDateTimeInstance(java.text.DateFormat.MEDIUM,
                    java.text.DateFormat.SHORT).format(d);
            }
        } catch (java.text.ParseException ignored) {
            // Not a name this app gave it.
        }
        return name;
    }

    private void openPast(File f) {
        if (proc != null) return;
        String raw;
        try {
            raw = readFile(f);
        } catch (IOException e) {
            status.setText("Could not read " + f.getName() + ": " + e.getMessage());
            return;
        }
        List<JSONObject> docs = parseDocs(raw);
        if (docs.isEmpty()) {
            status.setText(f.getName() + " holds no result.");
            return;
        }
        lastRaw = raw;
        lastName = f.getName();
        showResults(docs, 0);
        saveButton.setEnabled(true);
        shareButton.setEnabled(true);
        warning.setVisibility(View.GONE);
        compareButton.setVisibility(View.GONE);
        JSONArray kept = readReceipt(lastName);
        JSONObject first = kept.optJSONObject(0);
        String page = first == null || first.optBoolean("withdrawn") ? "" : first.optString("page", "");
        if (!page.isEmpty()) {
            compareButton.setOnClickListener(v -> openPage(page));
            compareButton.setVisibility(View.VISIBLE);
        }
        showWithdraw(lastName);
        status.setText(when(lastName) + (kept.length() == 0 ? ""
            : first != null && first.optBoolean("withdrawn") ? " · withdrawn" : " · uploaded"));
        scroll.post(() -> scroll.smoothScrollTo(0, Math.max(0, status.getTop() - dp(8))));
    }

    private static String readFile(File f) throws IOException {
        try (InputStream in = new FileInputStream(f)) {
            java.io.ByteArrayOutputStream buf = new java.io.ByteArrayOutputStream();
            byte[] chunk = new byte[8192];
            for (int n; (n = in.read(chunk)) > 0; ) buf.write(chunk, 0, n);
            return buf.toString("UTF-8");
        }
    }

    private void openPage(String url) {
        try {
            startActivity(new Intent(Intent.ACTION_VIEW, Uri.parse(url)));
        } catch (android.content.ActivityNotFoundException e) {
            status.setText("No browser to open " + url);
        }
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
        results.addView(note("Tap a heading for details. Score is the geometric mean "
            + "of the bold columns."));
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

    /** The bar across the top: the icon, the name and what it is, on the
     *  hub's green, which also fills in behind the status bar. */
    private View header() {
        LinearLayout bar = new LinearLayout(this);
        bar.setGravity(Gravity.CENTER_VERTICAL);
        bar.setBackgroundColor(getColor(R.color.header));
        bar.setPadding(dp(16), dp(12), dp(16), dp(12));
        bar.setElevation(dp(4));
        ImageView icon = new ImageView(this);
        icon.setImageResource(R.mipmap.ic_launcher);
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(dp(40), dp(40));
        lp.setMarginEnd(dp(14));
        bar.addView(icon, lp);
        LinearLayout words = column();
        TextView name = text(20, true);
        name.setText("cpcpub");
        name.setTextColor(0xFFFFFFFF);
        name.setLetterSpacing(0.02f);
        words.addView(name);
        TextView what = text(13, false);
        what.setText("Cross-platform CPU benchmark");
        what.setTextColor(0xCCFFFFFF);
        words.addView(what);
        bar.addView(words);
        return bar;
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

    /** 0 for both, 1 for the multi-threaded run alone, 2 for per-core alone. */
    private int mode() {
        return multi.isChecked() && perCore.isChecked() ? 0 : perCore.isChecked() ? 2 : 1;
    }

    private void setModes(boolean m, boolean p) {
        syncingModes = true;
        multi.setChecked(m);
        perCore.setChecked(p);
        both.setChecked(m && p);
        syncingModes = false;
        updateCommand();
    }

    /** One of the two changed; other is the one that did not. */
    private void onModeChecked(CheckBox other) {
        if (syncingModes) return;
        // Unticking the last ticked one moves the tick to the other rather than
        // refusing: a tap should always change something.
        if (!multi.isChecked() && !perCore.isChecked()) {
            setModes(other == multi, other == perCore);
            return;
        }
        setModes(multi.isChecked(), perCore.isChecked());
    }

    /** A button that looks like one, in a row of them. */
    private Button button(LinearLayout row, String label) {
        Button b = new Button(this);
        b.setText(label);
        b.setAllCaps(false);
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(
            LinearLayout.LayoutParams.WRAP_CONTENT, LinearLayout.LayoutParams.WRAP_CONTENT);
        lp.setMarginEnd(dp(8));
        row.addView(b, lp);
        return b;
    }

    private CheckBox check(LinearLayout parent, String label, int indent) {
        CheckBox c = new CheckBox(this);
        c.setText(label);
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(
            LinearLayout.LayoutParams.WRAP_CONTENT, LinearLayout.LayoutParams.WRAP_CONTENT);
        lp.setMarginStart(indent);
        parent.addView(c, lp);
        return c;
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

    /** A Show/Hide button beside a password field, to check what was pasted. */
    private void revealable(EditText e) {
        LinearLayout parent = (LinearLayout) e.getParent();
        int at = parent.indexOfChild(e);
        parent.removeView(e);
        LinearLayout row = new LinearLayout(this);
        row.setGravity(Gravity.CENTER_VERTICAL);
        row.addView(e, new LinearLayout.LayoutParams(0,
            ViewGroup.LayoutParams.WRAP_CONTENT, 1f));
        Button toggle = new Button(this, null, android.R.attr.borderlessButtonStyle);
        toggle.setText("Show");
        toggle.setAllCaps(false);
        toggle.setOnClickListener(v -> {
            boolean shown = toggle.getText().equals("Hide");
            e.setInputType(InputType.TYPE_CLASS_TEXT | (shown
                ? InputType.TYPE_TEXT_VARIATION_PASSWORD
                : InputType.TYPE_TEXT_VARIATION_VISIBLE_PASSWORD));
            e.setSelection(e.getText().length());
            toggle.setText(shown ? "Show" : "Hide");
        });
        row.addView(toggle);
        parent.addView(row, at);
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
