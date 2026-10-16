// Read the current Browser account. Never modify login or submit reward events.
rpc.exports = {
  identity() {
    return Java.performNow(function () {
      const manager = Java.use('com.vivo.content.common.account.AccountManager').getInstance();
      const account = manager.getAccountInfo();
      return {openid: manager.isLogined() && account ? String(account.openId.value) : ''};
    });
  },
  notifications() {
    return Java.performNow(function () {
      const app = Java.use('android.app.ActivityThread').currentApplication();
      const pkg = String(app.getPackageName());
      if (pkg !== 'com.vivo.browser') return [];
      const manager = Java.cast(app.getSystemService('notification'), Java.use('android.app.NotificationManager'));
      const label = String(app.getPackageManager().getApplicationLabel(app.getApplicationInfo()));
      const active = manager.getActiveNotifications();
      const found = [];
      for (let i = 0; i < active.length && found.length < 10; i++) {
        const row = active[i];
        if (String(row.getPackageName()) !== pkg || row.isOngoing()) continue;
        const notification = row.getNotification();
        if (!notification.contentIntent.value || (notification.flags.value & 0x200)) continue;
        const title = notification.extras.value.getCharSequence('android.title');
        if (title) found.push({id: row.getId(), title: String(title), app_label: label});
      }
      return found;
    });
  }
};
