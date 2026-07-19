<?php

namespace Vendor\Package\Models;

use Illuminate\Database\Eloquent\Model;

// a syntactically valid Eloquent model, but under vendor/ — must be excluded
// entirely, never surfacing as a table or contributing associations.
class VendorModel extends Model
{
    public function tags()
    {
        return $this->hasMany(Tag::class);
    }
}
