// Shared official account SDK, read only; the Python driver checks exact ownership.
rpc.exports = {
  identity() {
    return Java.performNow(function () {
      const account = Java.use('com.bbk.account.base.B').b();
      return {openid: account ? String(account.getOpenid()) : ''};
    });
  }
};
