// Getter chain from the original Game Center UserInfoManager, no forged login.
rpc.exports = {
  identity() {
    return new Promise((resolve, reject) => Java.perform(() => {
      try {
        const account = Java.use('com.vivo.game.core.account.s').h().j();
        resolve(account ? {openid: String(account.b())} : {});
      } catch (_) { reject('NATIVE_IDENTITY_UNAVAILABLE'); }
    }));
  }
};
