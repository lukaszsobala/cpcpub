package je.qd.cpcpub;

import android.text.Editable;
import android.text.TextWatcher;

/** A TextWatcher that only wants to hear that the text changed. */
final class SimpleWatcher implements TextWatcher {
    private final Runnable onChange;

    SimpleWatcher(Runnable onChange) {
        this.onChange = onChange;
    }

    @Override
    public void beforeTextChanged(CharSequence s, int start, int count, int after) {}

    @Override
    public void onTextChanged(CharSequence s, int start, int before, int count) {}

    @Override
    public void afterTextChanged(Editable s) {
        onChange.run();
    }
}
