package je.qd.cpcpub;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.os.Build;
import android.os.IBinder;
import android.os.PowerManager;

/**
 * Held for as long as a run measures, and does no work of its own. Without it,
 * leaving the app makes it a cached process, which Android freezes -- and the
 * benchmark, a child of the app, freezes with it, mid-measurement. A
 * foreground service keeps the app out of the cache; the partial wake lock
 * keeps the CPU awake if the screen goes off. The window keeps the screen on
 * by itself while the app is in front.
 */
public final class RunService extends Service {
    private static final String CHANNEL = "run";
    // A backstop only: the activity stops the service when the run ends.
    private static final long LOCK_MS = 6L * 60 * 60 * 1000;
    private PowerManager.WakeLock lock;

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        NotificationManager nm = getSystemService(NotificationManager.class);
        nm.createNotificationChannel(new NotificationChannel(CHANNEL, "Benchmark runs",
            NotificationManager.IMPORTANCE_LOW));
        PendingIntent open = PendingIntent.getActivity(this, 0,
            new Intent(this, MainActivity.class).addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP),
            PendingIntent.FLAG_IMMUTABLE);
        Notification n = new Notification.Builder(this, CHANNEL)
            .setSmallIcon(R.drawable.ic_stat_run)
            .setContentTitle("Measuring")
            .setContentText("Keep cpcpub on screen for full-speed results.")
            .setContentIntent(open)
            .setOngoing(true)
            .build();
        if (Build.VERSION.SDK_INT >= 34) {
            startForeground(1, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE);
        } else {
            startForeground(1, n);
        }
        if (lock == null) {
            lock = getSystemService(PowerManager.class)
                .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "cpcpub:run");
            lock.acquire(LOCK_MS);
        }
        return START_NOT_STICKY;
    }

    @Override
    public void onDestroy() {
        if (lock != null && lock.isHeld()) lock.release();
        lock = null;
        super.onDestroy();
    }
}
