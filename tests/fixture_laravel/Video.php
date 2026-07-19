<?php

namespace App\Models;

use Illuminate\Database\Eloquent\Model;

class Video extends Model
{
    public function thumbnail()
    {
        return $this->morphOne(Image::class, 'imageable');
    }

    public function relatedTags()
    {
        return $this->morphToMany(Tag::class, 'taggable');
    }
}
